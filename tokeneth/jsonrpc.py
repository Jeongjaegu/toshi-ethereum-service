import binascii
from tokenservices.jsonrpc.handlers import JsonRPCBase, map_jsonrpc_arguments
from tokenservices.jsonrpc.errors import JsonRPCInvalidParamsError, JsonRPCInternalError, JsonRPCError
from tokenservices.database import DatabaseMixin
from tokenservices.ethereum.mixin import EthereumMixin
from tokenservices.redis import RedisMixin
from tokenservices.ethereum.utils import data_decoder, data_encoder
from ethereum.exceptions import InvalidTransaction
from functools import partial
from tornado.ioloop import IOLoop
from tokenservices.utils import (
    validate_address, parse_int, validate_signature, validate_transaction_hash
)
from tokenservices.ethereum.tx import (
    DEFAULT_STARTGAS, DEFAULT_GASPRICE, create_transaction,
    encode_transaction, decode_transaction, is_transaction_signed,
    signature_from_transaction, add_signature_to_transaction,
    transaction_to_json
)

from tokenservices.log import log

from .mixins import BalanceMixin
from .utils import RedisLock

class JsonRPCInsufficientFundsError(JsonRPCError):
    def __init__(self, *, request=None, data=None):
        super().__init__(request.get('id') if request else None,
                         -32000, "Insufficient Funds", data,
                         'id' not in request if request else False)


class TokenEthJsonRPC(JsonRPCBase, BalanceMixin, DatabaseMixin, EthereumMixin, RedisMixin):

    def __init__(self, user_token_id, application):
        self.user_token_id = user_token_id
        self.application = application

    async def get_balance(self, address):

        if not validate_address(address):
            raise JsonRPCInvalidParamsError(data={'id': 'invalid_address', 'message': 'Invalid Address'})

        confirmed, unconfirmed = await self.get_balances(address)

        return {
            "confirmed_balance": hex(confirmed),
            "unconfirmed_balance": hex(unconfirmed)
        }

    @map_jsonrpc_arguments({'from': 'from_address', 'to': 'to_address'})
    async def create_transaction_skeleton(self, *, to_address, from_address, value=0, nonce=None, gas=None, gas_price=None, data=None):

        if not validate_address(from_address):
            raise JsonRPCInvalidParamsError(data={'id': 'invalid_from_address', 'message': 'Invalid From Address'})

        if not validate_address(to_address):
            raise JsonRPCInvalidParamsError(data={'id': 'invalid_to_address', 'message': 'Invalid To Address'})

        if value:
            value = parse_int(value)
            if value is None:
                raise JsonRPCInvalidParamsError(data={'id': 'invalid_value', 'message': 'Invalid Value'})

        # check optional arguments

        if nonce is None:
            # check cache for nonce
            nonce = self.redis.get("nonce:{}".format(from_address))
            if nonce:
                nonce = int(nonce)
            # get the network's value too
            nw_nonce = await self.eth.eth_getTransactionCount(from_address)
            if nonce is None or nw_nonce > nonce:
                # if not cached, or the cached value is lower than
                # the network value, use the network value!
                nonce = nw_nonce
        else:
            nonce = parse_int(nonce)
            if nonce is None:
                raise JsonRPCInvalidParamsError(data={'id': 'invalid_nonce', 'message': 'Invalid Nonce'})

        if data is not None:
            if isinstance(data, int):
                data = hex(data)
            if isinstance(data, str):
                try:
                    data = data_decoder(data)
                except binascii.Error:
                    pass
            if not isinstance(data, bytes):
                raise JsonRPCInvalidParamsError(data={'id': 'invalid_data', 'message': 'Invalid Data field'})
        else:
            data = b''

        if gas is None:
            # if there is data the default startgas value wont be enough
            if data:
                gas = await self.eth.eth_estimateGas(from_address, to_address, nonce=nonce, data=data)
            else:
                gas = DEFAULT_STARTGAS
        else:
            gas = parse_int(gas)
            if gas is None:
                raise JsonRPCInvalidParamsError(data={'id': 'invalid_gas', 'message': 'Invalid Gas'})

        if gas_price is None:
            gas_price = DEFAULT_GASPRICE
        else:
            gas_price = parse_int(gas_price)
            if gas_price is None:
                raise JsonRPCInvalidParamsError(data={'id': 'invalid_gas_price', 'message': 'Invalid Gas Price'})

        try:
            tx = create_transaction(nonce=nonce, gasprice=gas_price, startgas=gas,
                                    to=to_address, value=value, data=data)
        except InvalidTransaction as e:
            raise JsonRPCInvalidParamsError(data={'id': 'invalid_transaction', 'message': str(e)})

        transaction = encode_transaction(tx)

        return transaction

    async def send_transaction(self, *, tx, signature=None):

        try:
            tx = decode_transaction(tx)
        except:
            raise JsonRPCInvalidParamsError(data={'id': 'invalid_transaction', 'message': 'Invalid Transaction'})

        if is_transaction_signed(tx):

            if signature:

                tx_sig = signature_from_transaction(tx)
                if tx_sig != signature:

                    raise JsonRPCInvalidParamsError(data={
                        'id': 'invalid_signature',
                        'message': 'Invalid Signature: Signature in payload and signature of transaction do not match'
                    })
        else:

            if signature is None:
                raise JsonRPCInvalidParamsError(data={'id': 'missing_signature', 'message': 'Missing Signature'})

            if not validate_signature(signature):
                raise JsonRPCInvalidParamsError(data={
                    'id': 'invalid_signature',
                    'message': 'Invalid Signature: {}'.format(
                        'Invalid length' if len(signature) != 132 else 'Invalid hex value')
                })

            try:
                signature = data_decoder(signature)
            except Exception:
                log.exception("Unexpected error decoding valid signature: {}".format(signature))
                raise JsonRPCInvalidParamsError(data={
                    'id': 'invalid_signature',
                    'message': 'Invalid Signature'
                })

            add_signature_to_transaction(tx, signature)

        from_address = data_encoder(tx.sender)
        to_address = data_encoder(tx.to)

        # prevent spamming of transactions with the same nonce from the same sender
        with RedisLock(self.redis, "{}:{}".format(from_address, tx.nonce),
                       raise_when_locked=partial(JsonRPCInvalidParamsError, data={'id': 'invalid_nonce', 'message': 'Nonce already used'}),
                       ex=5):

            # disallow transaction overwriting for known transactions
            async with self.db:
                existing = await self.db.fetchrow("SELECT * FROM transactions WHERE "
                                                  "from_address = $1 AND nonce = $2 AND last_status != $3",
                                                  from_address, tx.nonce, 'error')
            if existing:
                # debugging checks
                existing_tx = await self.eth.eth_getTransactionByHash(existing['transaction_hash'])
                raise JsonRPCInvalidParamsError(data={'id': 'invalid_nonce', 'message': 'Nonce already used'})

            # make sure the account has enough funds for the transaction
            network_balance, balance = await self.get_balances(from_address, ignore_pending_recieved=True)

            log.info("Attempting to send transaction\n{} -> {}\nValue: {} + {} (gas) * {} (startgas) = {}\nSender's Balance {} ({} unconfirmed)".format(
                from_address, to_address, tx.value, tx.startgas, tx.gasprice, tx.value + (tx.startgas * tx.gasprice), network_balance, balance))

            if balance < (tx.value + (tx.startgas * tx.gasprice)):
                raise JsonRPCInsufficientFundsError(data={'id': 'insufficient_funds', 'message': 'Insufficient Funds'})

            # validate the nonce
            c_nonce = self.redis.get("nonce:{}".format(from_address))
            if c_nonce:
                c_nonce = int(c_nonce)
            # get the network's value too
            nw_nonce = await self.eth.eth_getTransactionCount(from_address)
            if c_nonce is None or nw_nonce > c_nonce:
                c_nonce = nw_nonce

            if tx.nonce < c_nonce:
                raise JsonRPCInvalidParamsError(data={'id': 'invalid_nonce', 'message': 'Provided nonce is too low'})
            if tx.nonce > c_nonce:
                raise JsonRPCInvalidParamsError(data={'id': 'invalid_nonce', 'message': 'Provided nonce is too high'})

            # send the transaction to the network
            try:
                tx_encoded = encode_transaction(tx)
                tx_hash = await self.eth.eth_sendRawTransaction(tx_encoded)
            except JsonRPCError as e:
                log.error(e.format())
                raise JsonRPCInternalError(data={
                    'id': 'unexpected_error',
                    'message': 'An error occured communicating with the ethereum network, try again later'
                })

            # cache nonce
            self.redis.set("nonce:{}".format(from_address), tx.nonce + 1)
            # add tx to database
            async with self.db:
                await self.db.execute(
                    "INSERT INTO transactions "
                    "(transaction_hash, from_address, to_address, nonce, value, estimated_gas_cost, sender_token_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    tx_hash, from_address, to_address, tx.nonce, str(tx.value), str(tx.startgas * tx.gasprice), self.user_token_id)
                await self.db.commit()

            # if there is a block monitor, force send PNs for this without
            # waiting for the node to see it
            if hasattr(self.application, 'monitor'):
                txjson = transaction_to_json(tx)
                assert txjson['hash'] == tx_hash
                IOLoop.current().add_callback(self.application.monitor.send_transaction_notifications, txjson)

        return tx_hash

    async def get_transaction(self, tx_hash):

        if not validate_transaction_hash(tx_hash):
            raise JsonRPCInvalidParamsError(data={'id': 'invalid_transaction_hash', 'message': 'Invalid Transaction Hash'})

        tx = await self.eth.eth_getTransactionByHash(tx_hash)
        return tx

    async def subscribe(self, *addresses):

        insert_args = []
        for address in addresses:
            if not validate_address(address):
                raise JsonRPCInvalidParamsError(data={'id': 'bad_arguments', 'message': 'Bad Arguments'})
            insert_args.extend([self.user_token_id, address])

        async with self.db:

            await self.db.execute(
                "INSERT INTO notification_registrations VALUES {} ON CONFLICT DO NOTHING".format(
                    ', '.join('(${}, ${})'.format((i * 2) + 1, (i * 2) + 2) for i, _ in enumerate(addresses))),
                *insert_args)

            await self.db.commit()

        return True

    async def unsubscribe(self, *addresses):

        for address in addresses:
            if not validate_address(address):
                raise JsonRPCInvalidParamsError(data={'id': 'bad_arguments', 'message': 'Bad Arguments'})

        async with self.db:

            await self.db.execute(
                "DELETE FROM notification_registrations WHERE token_id = $1 AND ({})".format(
                    ' OR '.join('eth_address = ${}'.format(i + 2) for i, _ in enumerate(addresses))),
                self.user_token_id, *addresses)

            await self.db.commit()

        return True

    async def list_subscriptions(self):

        async with self.db:

            rows = await self.db.fetch(
                "SELECT eth_address FROM notification_registrations WHERE token_id = $1",
                self.user_token_id)

        return [row['eth_address'] for row in rows]
