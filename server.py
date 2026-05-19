import asyncio
from aiohttp import web
import json
import random
import string
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = web.Application()

# Game state
rooms = {}

def generate_code():
    return ''.join(random.choices(string.digits, k=4))

def _clean_players(players_dict):
    res = {}
    for k, v in players_dict.items():
        res[k] = {k2: v2 for k2, v2 in v.items() if k2 != 'ws'}
    return res

async def broadcast(code, event, data, exclude_sid=None):
    if code in rooms:
        payload = json.dumps({'type': event, 'data': data})
        for sid, p in rooms[code]['players'].items():
            if sid != exclude_sid:
                try:
                    await p['ws'].send_str(payload)
                except Exception:
                    pass

async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    sid = str(id(ws))
    logger.info(f"Player {sid} connected")
    
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                    event = payload.get('type')
                    data = payload.get('data', {})
                    
                    if event == 'create_room':
                        username = data.get('username', 'Player')
                        code = generate_code()
                        while code in rooms:
                            code = generate_code()
                        
                        rooms[code] = {
                            'players': {
                                sid: {'name': username, 'ability': 'none', 'ready': False, 'is_host': True, 'ws': ws}
                            },
                            'started': False
                        }
                        await ws.send_json({'type': 'room_created', 'data': {'code': code, 'players': _clean_players(rooms[code]['players'])}})
                        logger.info(f"Room {code} created by {username} ({sid})")
                        
                    elif event == 'join_room':
                        code = data.get('code')
                        username = data.get('username', 'Player')
                        
                        if code in rooms:
                            if rooms[code]['started']:
                                await ws.send_json({'type': 'error', 'data': {'message': 'Game already started'}})
                            else:
                                rooms[code]['players'][sid] = {'name': username, 'ability': 'none', 'ready': False, 'is_host': False, 'ws': ws}
                                await ws.send_json({'type': 'joined_room', 'data': {'code': code, 'players': _clean_players(rooms[code]['players'])}})
                                await broadcast(code, 'lobby_update', {'players': _clean_players(rooms[code]['players'])})
                                logger.info(f"Player {username} ({sid}) joined room {code}")
                        else:
                            await ws.send_json({'type': 'error', 'data': {'message': 'Invalid room code'}})
                            logger.warning(f"Player {username} ({sid}) tried to join invalid room {code}")
                            
                    elif event == 'update_ability':
                        code = data.get('code')
                        ability = data.get('ability')
                        ready = data.get('ready', False)
                        if code in rooms and sid in rooms[code]['players']:
                            rooms[code]['players'][sid]['ability'] = ability
                            rooms[code]['players'][sid]['ready'] = ready
                            logger.debug(f"Lobby {code}: {rooms[code]['players'][sid]['name']} is {'READY' if ready else 'NOT READY'} ({ability})")
                            await broadcast(code, 'lobby_update', {'players': _clean_players(rooms[code]['players'])})
                            
                    elif event == 'start_game':
                        code = data.get('code')
                        if code in rooms and rooms[code]['players'][sid]['is_host']:
                            players = rooms[code]['players']
                            if len(players) >= 2 and all(p['ready'] for p in players.values()):
                                for p in players.values():
                                    p['dead'] = False
                                    p['death_time'] = None
                                    p['ready'] = False
                                rooms[code]['started'] = True
                                boss_id = random.choice([1, 2, 3])
                                seed = random.randint(0, 1000000)
                                await broadcast(code, 'game_start', {'boss_id': boss_id, 'seed': seed})
                                await broadcast(code, 'lobby_update', {'players': _clean_players(rooms[code]['players'])})
                                logger.info(f"Game started in room {code} (Boss: {boss_id}, Seed: {seed})")
                            else:
                                await ws.send_json({'type': 'error', 'data': {'message': 'Need 2+ players and everyone must choose an ability'}})
                                
                    elif event == 'admin_set_phase':
                        code = data.get('code')
                        if code in rooms:
                            await broadcast(code, 'set_phase', {'phase': data.get('phase')})
                                
                    elif event == 'player_update':
                        code = data.get('code')
                        if code in rooms:
                            if sid in rooms[code]['players']:
                                rooms[code]['players'][sid]['hp'] = data.get('hp', 0)
                                was_dead = rooms[code]['players'][sid].get('dead', False)
                                now_dead = data.get('dead', False)
                                rooms[code]['players'][sid]['dead'] = now_dead
                                
                                if now_dead and not was_dead:
                                    rooms[code]['players'][sid]['death_time'] = time.time()
                                    logger.info(f"Player {rooms[code]['players'][sid]['name']} ({sid}) died in room {code}")
                                    
                                    alive = [p for p in rooms[code]['players'].values() if not p.get('dead')]
                                    if len(alive) == 0:
                                        import copy
                                        results_data = _clean_players(rooms[code]['players'])
                                        await broadcast(code, 'match_over', {'players': results_data})
                                        rooms[code]['started'] = False
                                        
                            await broadcast(code, 'player_moved', {
                                'sid': sid,
                                'pos': data.get('pos'),
                                'hp': data.get('hp'),
                                'dead': data.get('dead', False)
                            }, exclude_sid=sid)
                            
                    elif event == 'slow_mo_update':
                        code = data.get('code')
                        active = data.get('active', False)
                        if code in rooms:
                            await broadcast(code, 'slow_mo_sync', {'sid': sid, 'active': active}, exclude_sid=None)
                            
                except Exception as e:
                    logger.error(f"Error parsing message: {e}")
                    
    finally:
        for code, room in list(rooms.items()):
            if sid in room['players']:
                name = room['players'][sid]['name']
                was_host = room['players'][sid]['is_host']
                del room['players'][sid]
                
                if not room['players']:
                    del rooms[code]
                    logger.info(f"Room {code} deleted (empty)")
                else:
                    if was_host:
                        new_host_sid = next(iter(room['players']))
                        room['players'][new_host_sid]['is_host'] = True
                    await broadcast(code, 'lobby_update', {'players': _clean_players(room['players'])})
                
                logger.info(f"Player {name} ({sid}) disconnected from room {code}")
                break
                
    return ws

app.router.add_get('/', handle_ws)
app.router.add_get('/ws', handle_ws)
# Also listen on socket.io for legacy compat testing locally
app.router.add_get('/socket.io/', handle_ws)

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Multiplayer Server (WebSockets) starting on port {port}...")
    web.run_app(app, port=port)
