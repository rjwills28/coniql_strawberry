import asyncio
import base64
import json
from argparse import ArgumentParser
from enum import Enum, auto
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import aiohttp_cors
import numpy as np
import strawberry
from aiohttp import web
from strawberry.aiohttp.views import GraphQLView
from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL, GRAPHQL_WS_PROTOCOL
from strawberry.types import Info

from .caplugin import CAPlugin
from .device_config import ChannelConfig
from .plugin import PluginStore
from .pvaplugin import PVAPlugin
from .simplugin import SimPlugin
from .types import Channel, ChannelValue


# Resolver dependencies
class DeferredChannel:
    id: str
    lock: asyncio.Lock
    channel: Optional[Channel] = None

    async def populate_channel(self) -> Channel:
        raise NotImplementedError(self)

    async def get_channel(self) -> Channel:
        if self.channel is None:
            async with self.lock:
                # If channel is still None we should make it
                if self.channel is None:
                    self.channel = await self.populate_channel()
        assert self.channel
        return self.channel


NO_CONFIG = ChannelConfig(name="")


class GetChannel(DeferredChannel):
    def __init__(self, channel_id: str, timeout: float, store: PluginStore):
        self.plugin, self.config, self.id = store.plugin_config_id(channel_id)
        # Remove the transport prefix from the read pv
        self.pv = store.transport_pv(self.config.read_pv or self.config.write_pv)[1]
        self.timeout = timeout
        self.lock = asyncio.Lock()

    async def populate_channel(self) -> Channel:
        channel = await self.plugin.get_channel(self.pv, self.timeout, self.config)
        return channel


class SubscribeChannel(DeferredChannel):
    def __init__(self, channel_id: str, channel: Channel):
        self.id = channel_id
        self.channel = channel


# Resolvers
async def get_channel(id, timeout, ctx) -> DeferredChannel:
    return GetChannel(id, timeout, ctx["store"])


async def channel_value(parent: DeferredChannel) -> Optional[ChannelValue]:
    channel = await parent.get_channel()
    return channel.get_value()


async def channel_value_float(parent: ChannelValue) -> Optional[float]:
    return parent.formatter.to_float(parent.value)


async def subscribe_channel(id, ctx) -> AsyncIterator[Any]:
    store: PluginStore = ctx["store"]
    plugin, config, channel_id = store.plugin_config_id(id)
    # Remove the transport prefix from the read pv
    pv = store.transport_pv(config.read_pv)[1]
    async for channel in plugin.subscribe_channel(pv, config):
        yield SubscribeChannel(channel_id, channel)
        # yield dict(subscribeChannel=SubscribeChannel(channel_id, channel))


async def put_channel(
    ids: List[str], put_values: List[str], timeout, ctx
) -> Sequence[DeferredChannel]:
    store: PluginStore = ctx["store"]
    pvs = []
    plugins = set()
    for channel_id in ids:
        plugin, config, channel_id = store.plugin_config_id(channel_id)
        pv = config.write_pv
        assert pv, f"{channel_id} is configured read-only"
        plugins.add(plugin)
        pvs.append(store.transport_pv(pv)[1])
    values = []
    for value in put_values:
        if value[:1] in "[{":
            # need to json decode
            value = json.loads(value)
            if isinstance(value, dict):
                # decode base64 array
                dtype = np.dtype(value["numberType"].lower())
                value_b = base64.b64decode(value["base64"])
                # https://stackoverflow.com/a/6485943
                value = np.frombuffer(value_b, dtype=dtype)
        values.append(value)
    assert len(values) == len(pvs), "Mismatch in ids and values length"
    assert len(plugins) == 1, "Can only put to pvs with the same transport, not %s" % [
        p.transport for p in plugins
    ]
    await plugins.pop().put_channels(pvs, values, timeout)
    channels = [GetChannel(channel_id, timeout, store) for channel_id in ids]
    return channels


# Schema
@strawberry.type
class Range:
    # "The minimum number that is in this range"
    min: float
    # "The maximum that is in this range"
    max: float


@strawberry.enum
class ChannelQuality(Enum):
    # "Value is known, valid, nothing is wrong"
    VALID = auto()
    # "Value is known, valid, but is in the range generating a warning"
    WARNING = auto()
    # "Value is known, valid, but is in the range generating an alarm condition"
    ALARM = auto()
    # "Value is known, but not valid, e.g. a RW before its first put"
    INVALID = auto()
    # "The value is unknown, for instance because the channel is disconnected"
    UNDEFINED = auto()
    # "The Channel is currently in the process of being changed"
    CHANGING = auto()


@strawberry.type
class ChannelValue:
    # "The current value formatted as a string"
    # @strawberry.field
    # def string(self,
    #    #"Whether to include the units in the string"
    #    units: bool = False) -> str

    # "The current value formatted as a Float, Null if not expressable"
    @strawberry.field
    def float(self) -> float:  # = strawberry.field(resolver=channel_value_float)
        return channel_value_float(self)


@strawberry.type
class ChannelStatus:
    # "Of what quality is the current Channel value"
    quality: ChannelQuality
    # "Free form text describing the current status"
    message: str
    # "Whether the Channel will currently accept mutations"
    mutable: bool


@strawberry.type
class ChannelTime:
    # "Floating point number of seconds since Jan 1, 1970 00:00:00 UTC"
    seconds: float
    # "A more accurate version of the nanoseconds part of the seconds field"
    nanoseconds: int
    # "An integer value whose interpretation is deliberately undefined"
    userTag: int
    # "The timestamp as a datetime object"
    # datetime: datetime


@strawberry.type
class ChannelDisplay:
    # "A human readable possibly multi-line description for a tooltip"
    description: str


@strawberry.type
class Channel:
    # "ID that uniquely defines this Channel, normally a PV"
    id: strawberry.ID

    # "The current value of this channel"
    @strawberry.field
    def value(self) -> ChannelValue:
        return channel_value(self)

    # "When was the value last updated"
    time: ChannelTime
    # "Status of the connection, whether is is mutable, and alarm info"
    status: ChannelStatus
    # "How should the Channel be displayed"
    display: ChannelDisplay


def make_context() -> Dict[str, Any]:
    store = PluginStore()
    store.add_plugin("ssim", SimPlugin())
    store.add_plugin("pva", PVAPlugin())
    store.add_plugin("ca", CAPlugin(), set_default=True)
    context = dict(store=store)
    return context


context = make_context()


class MyGraphQLView(GraphQLView):
    async def get_context(self, request: web.Request, response: web.StreamResponse):
        ctx = context
        return {"request": request, "response": response, "ctx": ctx}


@strawberry.type
class Query:
    # "Get the current value of a Channel"
    @strawberry.field
    def getChannel(
        self, info: Info, id: strawberry.ID, timeout: float = 5.0
    ) -> Channel:
        return get_channel(id, timeout, info.context["ctx"])


@strawberry.type
class Subscription:
    # "Subscribe to changes in top level fields of Channel,
    # if they haven't changed they will be Null"
    @strawberry.subscription
    async def subscribeChannel(self, info: Info, id: strawberry.ID) -> Channel:
        return subscribe_channel(id, info.context["ctx"])


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def putChannels(
        self,
        info: Info,
        ids: List[strawberry.ID],
        values: List[str],
        timeout: float = 5.0,
    ) -> List[Channel]:
        return await put_channel(ids, values, timeout, info.context["ctx"])


def main(args=None) -> None:
    """
    Entry point of the application.
    """
    parser = ArgumentParser(description="CONtrol system Interface over graphQL")
    parser.add_argument(
        "--cors", action="store_true", help="Allow CORS for all origins and routes"
    )
    parsed_args = parser.parse_args(args)
    schema = strawberry.Schema(
        query=Query, subscription=Subscription, mutation=Mutation
    )

    view = MyGraphQLView(
        schema=schema,
        subscription_protocols=[GRAPHQL_TRANSPORT_WS_PROTOCOL, GRAPHQL_WS_PROTOCOL],
    )

    app = web.Application()

    app.router.add_route("GET", "/ws", view)
    app.router.add_route("POST", "/graphql", view)

    if parsed_args.cors:
        # Enable CORS for all origins on all routes.
        cors = aiohttp_cors.setup(app)
        for route in app.router.routes():
            allow_all = {
                "*": aiohttp_cors.ResourceOptions(
                    allow_headers=("*"), max_age=3600, allow_credentials=True
                )
            }
            cors.add(route, allow_all)

    web.run_app(app)
