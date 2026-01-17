from typing import *
from enum import Enum
import asyncio
import json
import websockets
import threading
import signal
import requests
import functools
import re

class Method(Enum):
    GET = 'GET'
    POST = 'POST'

def bsapi_request(method: Method, prefix: str, path: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
    url = f'{prefix}{path}'
    resp = None
    match method:
        case Method.GET:
            resp = requests.get(url, params=params)
        case Method.POST:
            resp = requests.post(url, data=params)
    resp.encoding = 'utf-8'
    if resp.status_code != 200:
        return None
    content_type = resp.headers.get('Content-Type', '')
    return resp.json() if 'application/json' in content_type else {}

def bsapi(path: str, method: Method):
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Optional[dict[str, Any]]:
            params = {
                **(func(*args, **kwargs) or {})
            }
            return bsapi_request(method, 'https://api.beatleader.com', path.format(**kwargs), params)
        return wrapper
    return decorator

class BSAPI:
    @bsapi('/player/{id}', Method.GET)
    def _exists(id: str):
        pass

    @bsapi('/players', Method.GET)
    def players(**kwargs):
        return kwargs

    @bsapi('/player/{id}', Method.GET)
    def player(id: str):
        pass

    @bsapi('/player/{id}/accgraph', Method.GET)
    def accgraph(id: str, **kwargs):
        return kwargs

    def exists(id: str) -> bool:
        return BSAPI._exists(id=id) is not None

class RawCommand(NamedTuple):
    sender: int
    command: str
    targets: set[int]

class Command(NamedTuple):
    group: Optional[int]
    sender: int
    command: list[str]
    targets: set[int]

class BeatSaberQuery:
    PREFIX: str = '\\'

    available_commands: set[str]
    router: dict[str, Callable]
    group_enabled: set[int]
    private_enabled: set[int]

    _task_queue: asyncio.Queue[Command]
    _ws_server_addr: str
    _ws_server_port: int
    _http_server_addr: str
    _http_server_port: int
    _cancelled: threading.Event

    def __init__(self):
        self.available_commands = set()
        self.router = {}
        self.group_enabled = set()
        self.private_enabled = set()
        self._task_queue = asyncio.Queue()
        self._ws_server_addr = 'localhost'
        self._ws_server_port = 3001
        self._http_server_addr = 'localhost'
        self._http_server_port = 3000
        self._cancelled = threading.Event()

    def command(self, subcmd: str):
        def decorator(func: Callable):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                reply: Optional[str] = func(*args, **kwargs)
                private = kwargs.get('sender')
                group = kwargs.get('group')
                if reply and (private or group):
                    self.reply(reply, group=group, private=private)
            self.available_commands.add(subcmd)
            self.router[subcmd] = wrapper
        return decorator

    async def enqueue(self, task: Command):
        await self._task_queue.put(task)

    async def dequeue(self) -> Command:
        return await self._task_queue.get()

    def parse(self, text: str) -> Optional[list[str]]:
        argv = list(map(str.strip, filter(lambda e: e, text.split(' '))))
        if len(argv) == 0:
            return None
        if not argv[0].startswith(BeatSaberQuery.PREFIX):
            return None
        argv[0] = argv[0][len(BeatSaberQuery.PREFIX):]
        cmd = argv[0]
        if not cmd in self.available_commands:
            return None
        return argv

    def query(self, cmd: Command):
        self.router[cmd.command[0]](*cmd.command[1:], sender=cmd.sender, targets=cmd.targets, group=cmd.group)

    def translate(self, msg: dict[str, Any]) -> Optional[RawCommand]:
        allowd_type = ['at', 'text']
        if len(list(filter(lambda e: e not in allowd_type, map(lambda e: e['type'], msg['message'])))) != 0:
            return None
        sender = msg['user_id']
        command = ''.join(map(lambda e: e['data']['text'] if e['type'] == 'text' else ' ', msg['message']))
        targets = set(map(lambda e: e['data']['qq'], filter(lambda e: e['data']['qq'] != 'all', filter(lambda e: e['type'] == 'at', msg['message']))))
        return RawCommand(sender, command, targets)

    async def message_poller(self, addr: str, port: int):
        async with websockets.connect(f'ws://{addr}:{port}') as ws:
            while not self._cancelled.is_set():
                data = await ws.recv()
                try:
                    msg: dict[str, Any] = json.loads(data)
                except:
                    continue
                enabled = False
                match msg.get('message_type'):
                    case 'private':
                        enabled = msg['user_id'] in self.private_enabled
                    case 'group':
                        enabled = msg['group_id'] in self.group_enabled
                if not enabled:
                    continue
                opt_raw = self.translate(msg)
                if not opt_raw:
                    continue
                raw: RawCommand = opt_raw
                if command := self.parse(opt_raw.command):
                    group = msg.get('group_id')
                    await self.enqueue(Command(group, raw.sender, command, raw.targets))

    async def message_handler(self):
        while not self._cancelled.is_set():
            task = await self.dequeue()
            self.query(task)

    def reply(self, msg: str, **kwargs):
        group: Optional[int] = kwargs.get('group')
        private: Optional[int] = kwargs.get('private')
        if group:
            api = f'http://{self._http_server_addr}:{self._http_server_port}/send_group_msg'
            data = {
                'group_id': group,
                'message': f'[CQ:at,qq={private}] {msg}',
            }
        elif private:
            api = f'http://{self._http_server_addr}:{self._http_server_port}/send_private_msg'
            data = {
                'user_id': private,
                'message': msg,
            }
        if api and data:
            requests.post(api, json=data)

    def listen(self, addr: str, port: int):
        self._ws_server_addr = addr
        self._ws_server_port = port

    def server(self, addr: str, port: int):
        self._http_server_addr = addr
        self._http_server_port = port

    def exec(self):
        def cancel(signum, frame):
            self._cancelled.set()
        signal.signal(signal.SIGINT, cancel)
        signal.signal(signal.SIGTERM, cancel)
        async def main():
            try:
                producer = self.message_poller(self._ws_server_addr, self._ws_server_port)
                consumer = self.message_handler()
                await asyncio.gather(producer, consumer)
            except asyncio.CancelledError:
                pass
        asyncio.run(main())

bs = BeatSaberQuery()
bs.group_enabled.add(743972515)
bs.private_enabled.add(1745096608)

bindings: dict[int, int] = {}

@bs.command('help')
def display_help(*args, **kwargs):
    commands = {
        'help': f'显示帮助信息，输入 {bs.PREFIX}help <命令> 显示详细信息',
        'me': '查询或关联我的 BL 账号',
        'rkup': '查询打榜相关信息',
    }
    # TODO: details
    commands = {cmd: brief for cmd, brief in commands.items() if cmd.split(' ')[0] in bs.available_commands}
    if len(commands) == 0:
        return
    return '当前可用命令\n' + '\n'.join(f'{bs.PREFIX}{cmd} - {brief}' for cmd, brief in commands.items())

@bs.command('me')
def query_me(*args, **kwargs):
    opt_uid: Optional[str] = None
    sender = kwargs['sender']
    already_bind = sender in bindings
    if len(args) == 0:
        if not already_bind:
            return f'账号暂未关联，请输入 {bs.PREFIX}help 查询帮助信息'
        opt_uid = bindings[sender]
    if (uid := args[0]).isdigit():
        if not BSAPI.exists(id=uid):
            return '非法 UID'
        opt_uid = uid
    else:
        kw = ' '.join(args)
        if len(kw) < 3:
            return '搜索关键字过短'
        results = BSAPI.players(search=kw, countries='cn', count=5)
        if not results:
            return '搜索失败，请稍后再试'
        results = results['data']
        if len(results) == 0:
            return '搜索结果为空，请检查关键字'
        if len(results) > 1:
            candidates = map(lambda e: f"[{e['country']}] {e['name']} / {e['pp']}pp ({e['id']})", results)
            return f'已找到以下候选结果\n' + '\n'.join(map(lambda e: f'{e[0]+1:02d}. {e[1]}', enumerate(candidates)))
        opt_uid = results[0]['id']
    profile = BSAPI.player(id=opt_uid)
    if not profile:
        if already_bind:
            return f'已检索到 UID {opt_uid}，资料获取失败'
        else:
            return '资料获取失败'
    msg = []
    if already_bind and bindings[sender] != opt_uid:
        msg.append(f'已切换关联至 UID {opt_uid}')
    elif not already_bind:
        msg.append(f'已成功关联至 UID {opt_uid}')
    bindings[sender] = opt_uid
    name = profile['name']
    rank = profile['rank']
    pp = profile['pp']
    country = profile['country']
    country_rank = profile['countryRank']
    platform = profile['platform']
    msg.append(f'[{country}][{platform}] {name} {pp:.2f}pp 国区 {country_rank} 全球 {rank}')
    return '\n'.join(msg)

@bs.command('rkup')
def query_rank_up(*args, **kwargs):
    uid = bindings.get(kwargs['sender'])
    if not uid:
        return f'账号暂未关联，请输入 {bs.PREFIX}help 查询帮助信息'

    mode: Optional[str] = None
    rank: Optional[int] = None
    pp: Optional[float] = None
    nr: Optional[int] = None
    pp_each: Optional[float] = None

    if profile := BSAPI.player(id=uid):
        country = profile['country']
        cur_rank = profile['countryRank']
        cur_pp = profile['pp']
    else:
        return '资料获取失败，请稍后再试'

    if accgraph := BSAPI.accgraph(id=uid, leaderboardContext='general', type='weight', no_unranked_stars=''):
        pp_list = sorted(map(lambda e: e['pp'], accgraph), reverse=True)
    else:
        return 'PP 列表获取失败，请稍后再试'

    if len(args) == 0:
        nr = 1
        rank = cur_rank + 1
        mode = 'rank'

    if len(args) >= 1:
        if args[0].isdigit():
            nr = 1
            rank = int(args[0])
            mode = 'rank'
        elif re.compile(r'[+-]\d+').fullmatch(args[0]):
            nr = 1
            rank = cur_rank - int(args[0])
            mode = 'rank' if rank <= cur_rank else 'rank-reverse'
        elif re.compile(r'(\d+(\.\d*)?|\.\d+)pp').fullmatch(args[0]):
            nr = 1
            pp = float(args[0][:-2])
            mode = 'pp'
        elif re.compile(r'\+(\d+(\.\d*)?|\.\d+)pp').fullmatch(args[0]):
            nr = 1
            pp = cur_pp + float(args[0][:-2])
            mode = 'pp'
        else:
            return f'非法的输入 "{args[0]}"，请输入 {bs.PREFIX}help 查询帮助信息'

    if len(args) >= 2:
        if args[1].isdigit():
            nr = int(args[1])
        elif re.compile(r'(\d+(\.\d*)?|\.\d+)pp').fullmatch(args[0]):
            pp_each = float(args[1][:-2])
        else:
            return f'非法的输入 "{args[1]}"，请输入 {bs.PREFIX}help 查询帮助信息'

    if rank is not None and rank == cur_rank:
        return f'你已经是国区第 {rank} 名'

    if pp is not None and cur_pp >= pp:
        if f'{pp:.2f}' == f'{cur_pp:.2f}':
            return f'你已经达到 {pp:.2f}pp'
        else:
            return f'你已经达到 {pp:.2f}pp，目前为 {cur_pp:.2f}pp (+{cur_pp-pp:.2f})'

    if nr is not None and nr == 0 and mode != 'rank-reverse':
        return f'摆烂是无法上分的，你总得打一首歌'

    if resp := BSAPI.players(countries=country, order='asc', count=1, sortBy='pp'):
        total_players = resp['metadata']['total']
    else:
        return f'获取国区 {country} 的玩家信息失败'

    if mode == 'rank-reverse' and rank > total_players:
        return f'国区排名 {rank} 的玩家还没有出生'

    target_rank: Optional[int] = None
    target_name: Optional[str] = None
    target_pp: Optional[float] = None
    target_global_rank: Optional[int] = None
    if mode == 'rank' or mode == 'rank-reverse':
        target_rank = max(1, rank)
        per_page = 10
        if resp := BSAPI.players(countries=country, order='desc', count=per_page, page=(target_rank - 1) // per_page + 1, sortBy='pp'):
            player = resp['data'][target_rank % per_page - 1]
            target_name = player['name']
            target_pp = player['pp']
            target_global_rank = player['rank']
        else:
            return f'获取国区排名 {rank} 的玩家信息失败'

    # TODO: impl

    match mode:
        case 'rank':
            pass
        case 'pp':
            pass
        case 'rank-reverse':
            pass

bs.server(addr='localhost', port=3000)
bs.listen(addr='localhost', port=3001)
bs.exec()
