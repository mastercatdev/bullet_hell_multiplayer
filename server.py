import socketio
import asyncio
from aiohttp import web
import random
import string
import time
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Initialize Async Socket.IO server
sio = socketio.AsyncServer(cors_allowed_origins='*', async_mode='aiohttp')
app = web.Application()
sio.attach(app)

# Game state
rooms = {}

@sio.event
async def slow_mo_update(sid, data):
    code = data.get('code')
    active = data.get('active', False)
    if code in rooms:
        await sio.emit('slow_mo_sync', {'sid': sid, 'active': active}, room=code)

def generate_code():
    return ''.join(random.choices(string.digits, k=4))

@sio.event
async def connect(sid, environ):
    logger.info(f"Player {sid} connected")

@sio.event
async def create_room(sid, data):
    username = data.get('username', 'Player')
    code = generate_code()
    while code in rooms:
        code = generate_code()
    
    rooms[code] = {
        'players': {
            sid: {'name': username, 'ability': 'none', 'ready': False, 'is_host': True}
        },
        'started': False
    }
    await sio.enter_room(sid, code)
    await sio.emit('room_created', {'code': code, 'players': rooms[code]['players']}, room=sid)
    logger.info(f"Room {code} created by {username} ({sid})")

@sio.event
async def join_room(sid, data):
    code = data.get('code')
    username = data.get('username', 'Player')
    
    if code in rooms:
        if rooms[code]['started']:
            await sio.emit('error', {'message': 'Game already started'}, room=sid)
            return
        
        rooms[code]['players'][sid] = {'name': username, 'ability': 'none', 'ready': False, 'is_host': False}
        await sio.enter_room(sid, code)
        await sio.emit('joined_room', {'code': code, 'players': rooms[code]['players']}, room=sid)
        await sio.emit('lobby_update', {'players': rooms[code]['players']}, room=code)
        logger.info(f"Player {username} ({sid}) joined room {code}")
    else:
        await sio.emit('error', {'message': 'Invalid room code'}, room=sid)
        logger.warning(f"Player {username} ({sid}) tried to join invalid room {code}")

@sio.event
async def update_ability(sid, data):
    code = data.get('code')
    ability = data.get('ability')
    ready = data.get('ready', False)
    if code in rooms and sid in rooms[code]['players']:
        rooms[code]['players'][sid]['ability'] = ability
        rooms[code]['players'][sid]['ready'] = ready
        logger.debug(f"Lobby {code}: {rooms[code]['players'][sid]['name']} is {'READY' if ready else 'NOT READY'} ({ability})")
        await sio.emit('lobby_update', {'players': rooms[code]['players']}, room=code)

@sio.event
async def start_game(sid, data):
    code = data.get('code')
    if code in rooms and rooms[code]['players'][sid]['is_host']:
        players = rooms[code]['players']
        if len(players) >= 2 and all(p['ready'] for p in players.values()):
            # Reset player states for new round
            for p in players.values():
                p['dead'] = False
                p['death_time'] = None
            
            rooms[code]['started'] = True
            boss_id = random.choice([1, 2, 3])
            seed = random.randint(0, 1000000)
            await sio.emit('game_start', {'boss_id': boss_id, 'seed': seed}, room=code)
            logger.info(f"Game started in room {code} (Boss: {boss_id}, Seed: {seed})")
        else:
            await sio.emit('error', {'message': 'Need 2+ players and everyone must choose an ability'}, room=sid)

@sio.event
async def player_update(sid, data):
    code = data.get('code')
    if code in rooms:
        # Update internal state
        if sid in rooms[code]['players']:
            rooms[code]['players'][sid]['hp'] = data.get('hp', 0)
            was_dead = rooms[code]['players'][sid].get('dead', False)
            now_dead = data.get('dead', False)
            rooms[code]['players'][sid]['dead'] = now_dead
            
            if now_dead and not was_dead:
                rooms[code]['players'][sid]['death_time'] = time.time()
                logger.info(f"Player {rooms[code]['players'][sid]['name']} ({sid}) died in room {code}")
                
                # Check if game is over (all dead or only 1 survivor)
                players = rooms[code]['players']
                alive = [p for p in players.values() if not p.get('dead')]
                # Match finished only when NO ONE is alive
                if len(alive) == 0:
                    import copy
                    results_data = copy.deepcopy(players)
                    await sio.emit('match_over', {'players': results_data}, room=code)
                    rooms[code]['started'] = False

        # Broadcast player position to everyone else in the room
        await sio.emit('player_moved', {
            'sid': sid,
            'pos': data.get('pos'),
            'hp': data.get('hp'),
            'dead': data.get('dead', False)
        }, room=code, skip_sid=sid)

@sio.event
async def disconnect(sid):
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
                await sio.emit('lobby_update', {'players': room['players']}, room=code)
            
            logger.info(f"Player {name} ({sid}) disconnected from room {code}")
            break

if __name__ == '__main__':
    import os
    # Cloud providers like Render/Heroku provide the port via an environment variable
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Multiplayer Server (Async) starting on port {port}...")
    web.run_app(app, port=port)
