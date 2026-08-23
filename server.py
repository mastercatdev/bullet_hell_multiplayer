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

MAX_PLAYERS = 4

# Coop: the boss gets tougher with party size, but SUB-linearly, so a team
# out-damages it. That is the point of coop - it lets you attempt a boss
# above your own rating, with the contribution-weighted rating award
# stopping it from being a free ride.
def coop_hp_scale(n):
    return 1.0 + 0.6 * (max(1, n) - 1)

def generate_code():
    return ''.join(random.choices(string.digits, k=4))

def _clean_players(players_dict):
    res = {}
    for k, v in players_dict.items():
        res[k] = {k2: v2 for k2, v2 in v.items() if k2 != 'ws'}
    return res

def _room_config(room):
    return {'mode': room.get('mode', 'versus'),
            'boss_id': room.get('boss_id', 1),
            'starting_hp': room.get('starting_hp', 5)}


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
                                sid: {'name': username, 'ability': 'none', 'pclass': 'base', 'rating': 0, 'ready': False, 'is_host': True, 'ws': ws}
                            },
                            'started': False,
                            'mode': 'versus',       # 'versus' | 'coop'
                            'boss_id': 1,
                            'starting_hp': 5,
                            'coop': None,
                        }
                        await ws.send_json({'type': 'room_created', 'data': {'code': code, 'players': _clean_players(rooms[code]['players']), 'config': _room_config(rooms[code])}})
                        logger.info(f"Room {code} created by {username} ({sid})")
                        
                    elif event == 'join_room':
                        code = data.get('code')
                        username = data.get('username', 'Player')
                        
                        if code in rooms:
                            if rooms[code]['started']:
                                await ws.send_json({'type': 'error', 'data': {'message': 'Game already started'}})
                            elif len(rooms[code]['players']) >= MAX_PLAYERS:
                                await ws.send_json({'type': 'error', 'data': {'message': f'Room is full ({MAX_PLAYERS} players max)'}})
                            else:
                                rooms[code]['players'][sid] = {'name': username, 'ability': 'none', 'pclass': 'base', 'rating': 0, 'ready': False, 'is_host': False, 'ws': ws}
                                await ws.send_json({'type': 'joined_room', 'data': {'code': code, 'players': _clean_players(rooms[code]['players']), 'config': _room_config(rooms[code])}})
                                await broadcast(code, 'lobby_update', {'players': _clean_players(rooms[code]['players']), 'config': _room_config(rooms[code])})
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
                            if 'pclass' in data:
                                rooms[code]['players'][sid]['pclass'] = data.get('pclass', 'base')
                            if 'rating' in data:
                                # Coop gates on the party's LOWEST rating, so every
                                # member reports theirs to the lobby.
                                try:
                                    rooms[code]['players'][sid]['rating'] = int(data.get('rating', 0))
                                except (TypeError, ValueError):
                                    rooms[code]['players'][sid]['rating'] = 0
                            logger.debug(f"Lobby {code}: {rooms[code]['players'][sid]['name']} is {'READY' if ready else 'NOT READY'} ({ability})")
                            await broadcast(code, 'lobby_update', {'players': _clean_players(rooms[code]['players']), 'config': _room_config(rooms[code])})
                            
                    elif event == 'update_room_config':
                        # Host only: coop/versus, and for coop the boss and team HP.
                        code = data.get('code')
                        if code in rooms and rooms[code]['players'].get(sid, {}).get('is_host'):
                            room = rooms[code]
                            if 'mode' in data:        room['mode'] = 'coop' if data['mode'] == 'coop' else 'versus'
                            if 'boss_id' in data:     room['boss_id'] = max(1, min(5, int(data['boss_id'])))
                            if 'starting_hp' in data: room['starting_hp'] = max(1, min(5, int(data['starting_hp'])))
                            await broadcast(code, 'lobby_update', {'players': _clean_players(room['players']), 'config': _room_config(room)})

                    elif event == 'start_game':
                        code = data.get('code')
                        if code in rooms and rooms[code]['players'][sid]['is_host']:
                            room = rooms[code]
                            players = room['players']
                            if len(players) >= 2 and all(p['ready'] for p in players.values()):
                                for p in players.values():
                                    p['dead'] = False
                                    p['death_time'] = None
                                    p['ready'] = False
                                room['started'] = True
                                seed = random.randint(0, 1000000)
                                n = len(players)
                                if room.get('mode') == 'coop':
                                    boss_id = room.get('boss_id', 1)
                                    team_hp = room.get('starting_hp', 5)
                                    room['coop'] = {
                                        'boss_hp': None,      # filled by the first damage report
                                        'boss_max': None,
                                        'team_hp': team_hp,
                                        'contrib': {s: 0.0 for s in players},
                                        'over': False,
                                    }
                                    payload = {
                                        'boss_id': boss_id, 'seed': seed, 'mode': 'coop',
                                        'starting_hp': team_hp, 'players': n,
                                        'hp_scale': coop_hp_scale(n),
                                        'classes': {s: p.get('pclass', 'base') for s, p in players.items()},
                                    }
                                else:
                                    boss_id = random.choice([1, 2, 3, 4, 5])
                                    payload = {'boss_id': boss_id, 'seed': seed, 'mode': 'versus', 'players': n}
                                await broadcast(code, 'game_start', payload)
                                await broadcast(code, 'lobby_update', {'players': _clean_players(players), 'config': _room_config(room)})
                                logger.info(f"Game started in room {code} (mode={room.get('mode')}, Boss: {boss_id}, Seed: {seed}, n={n})")
                            else:
                                await ws.send_json({'type': 'error', 'data': {'message': 'Need 2+ players and everyone must be ready'}})

                    elif event == 'boss_damage':
                        # Coop: the server owns boss HP so every client agrees when
                        # it dies, and so damage contribution is a shared quantity.
                        code = data.get('code')
                        room = rooms.get(code)
                        if room and room.get('coop') and not room['coop']['over']:
                            co = room['coop']
                            if co['boss_max'] is None:
                                co['boss_max'] = float(data.get('boss_max', 1000))
                                co['boss_hp'] = co['boss_max']
                            dmg = max(0.0, float(data.get('dmg', 0)))
                            co['boss_hp'] = max(0.0, co['boss_hp'] - dmg)
                            co['contrib'][sid] = co['contrib'].get(sid, 0.0) + dmg
                            await broadcast(code, 'coop_state', {
                                'boss_hp': co['boss_hp'], 'boss_max': co['boss_max'],
                                'team_hp': co['team_hp'], 'contrib': co['contrib']})
                            if co['boss_hp'] <= 0:
                                co['over'] = True
                                room['started'] = False
                                await broadcast(code, 'coop_over', {
                                    'win': True, 'contrib': co['contrib'],
                                    'names': {s: p['name'] for s, p in room['players'].items()}})
                                logger.info(f"Coop win in room {code}")

                    elif event == 'team_hit':
                        # Shared HP pool: any player taking a hit drains the team.
                        code = data.get('code')
                        room = rooms.get(code)
                        if room and room.get('coop') and not room['coop']['over']:
                            co = room['coop']
                            try:
                                n_lost = max(1, int(data.get('n', 1)))
                            except (TypeError, ValueError):
                                n_lost = 1
                            co['team_hp'] = max(0, co['team_hp'] - n_lost)
                            await broadcast(code, 'coop_state', {
                                'boss_hp': co['boss_hp'] if co['boss_hp'] is not None else 0,
                                'boss_max': co['boss_max'] if co['boss_max'] is not None else 1,
                                'team_hp': co['team_hp'], 'contrib': co['contrib']})
                            if co['team_hp'] <= 0:
                                co['over'] = True
                                room['started'] = False
                                await broadcast(code, 'coop_over', {
                                    'win': False, 'contrib': co['contrib'],
                                    'names': {s: p['name'] for s, p in room['players'].items()}})
                                logger.info(f"Coop wipe in room {code}")
                                
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
                                'dead': data.get('dead', False),
                                'skin': data.get('skin'),
                                'shield': data.get('shield', False),
                                'wall': data.get('wall')
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
