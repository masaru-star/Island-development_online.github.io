import random
import string
import time
import asyncio
from typing import Dict, List, Optional
import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------
# サーバー設定
# ---------------------------------------------------------
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
sio_app = socketio.ASGIApp(sio, app)
app.mount("/", sio_app)

# ---------------------------------------------------------
# データ構造
# ---------------------------------------------------------
class Player:
    def __init__(self, sid, island_name, save_data):
        self.sid = sid
        self.island_name = island_name
        self.save_data = save_data # JSON string (encoded)
        self.is_turn_done = False
        self.action_queue = [] # 次のターンに実行する他島への干渉

class Room:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players: Dict[str, Player] = {} # sid -> Player
        self.turn = 0
        self.max_players = 7
        self.turn_deadline = 0.0 # タイムスタンプ
        self.timer_active = False

rooms: Dict[str, Room] = {}

# ---------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------
def generate_room_id():
    # 3桁の数字 + 2桁のアルファベット (例: 123AB)
    digits = "".join(random.choices(string.digits, k=3))
    chars = "".join(random.choices(string.ascii_uppercase, k=2))
    return digits + chars

async def check_turn_progression(room_id):
    """ターン進行判定"""
    room = rooms.get(room_id)
    if not room:
        return

    players = list(room.players.values())
    count = len(players)
    if count == 0:
        return

    done_count = sum(1 for p in players if p.is_turn_done)

    # 全員完了 OR (1人以上完了 AND 3分経過)
    should_proceed = False
    current_time = time.time()

    if done_count == count:
        should_proceed = True
    elif done_count > 0 and room.timer_active:
        if current_time >= room.turn_deadline:
            should_proceed = True

    if should_proceed:
        # ターン進行処理
        room.turn += 1
        room.timer_active = False
        
        # 各プレイヤーに「ターンを進めろ」という指令と、他プレイヤーからのアクションを送る
        # ※ここでアクションの解決などを行う
        
        # 全員のリセット
        for p in players:
            p.is_turn_done = False
        
        # クライアントへ通知
        await sio.emit('proceed_turn', {'turn': room.turn}, room=room_id)
        print(f"Room {room_id}: Turn proceeded to {room.turn}")

async def timer_loop():
    """3分タイマーの監視ループ"""
    while True:
        await asyncio.sleep(5)
        for room_id in list(rooms.keys()):
            await check_turn_progression(room_id)

# バックグラウンドタスクとしてタイマーを開始
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(timer_loop())

# ---------------------------------------------------------
# Socket.IO イベント
# ---------------------------------------------------------
@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")

@sio.event
async def create_room(sid, data):
    # data: { 'islandName': str, 'saveData': str }
    room_id = generate_room_id()
    while room_id in rooms:
        room_id = generate_room_id()
    
    rooms[room_id] = Room(room_id)
    new_player = Player(sid, data['islandName'], data['saveData'])
    rooms[room_id].players[sid] = new_player
    
    sio.enter_room(sid, room_id)
    await sio.emit('room_joined', {'roomId': room_id, 'isHost': True}, room=sid)
    print(f"Room created: {room_id} by {data['islandName']}")

@sio.event
async def join_room(sid, data):
    # data: { 'roomId': str, 'islandName': str, 'saveData': str }
    room_id = data['roomId']
    if room_id not in rooms:
        await sio.emit('error', {'message': '部屋が見つかりません'}, room=sid)
        return
    
    room = rooms[room_id]
    if len(room.players) >= room.max_players:
        await sio.emit('error', {'message': '部屋が満員です'}, room=sid)
        return

    new_player = Player(sid, data['islandName'], data['saveData'])
    room.players[sid] = new_player
    
    sio.enter_room(sid, room_id)
    await sio.emit('room_joined', {'roomId': room_id, 'isHost': False}, room=sid)
    
    # 参加者全員に通知
    await sio.emit('system_message', {'message': f"{data['islandName']}が入室しました"}, room=room_id)
    print(f"Player {data['islandName']} joined {room_id}")

@sio.event
async def update_state(sid, data):
    # ターン終了後の状態更新を受け取る
    # data: { 'roomId': str, 'saveData': str }
    room_id = data.get('roomId')
    if room_id in rooms and sid in rooms[room_id].players:
        rooms[room_id].players[sid].save_data = data['saveData']
        # ここで他プレイヤーへ状態同期を送ることも可能

@sio.event
async def get_player_list(sid, data):
    # 「他の島に行く」などで使用するリスト
    room_id = data.get('roomId')
    if room_id in rooms:
        players_list = []
        for pid, player in rooms[room_id].players.items():
            players_list.append({
                'sid': pid,
                'name': player.island_name,
                'saveData': player.save_data # 観光用データ
            })
        await sio.emit('player_list', {'players': players_list}, room=sid)

@sio.event
async def submit_turn_action(sid, data):
    # ターン終了ボタン押下時
    # data: { 'roomId': str, 'actionQueue': list, 'saveData': str }
    room_id = data.get('roomId')
    if room_id in rooms:
        room = rooms[room_id]
        player = room.players.get(sid)
        if player:
            player.save_data = data['saveData'] # 最新状態保存
            player.is_turn_done = True
            
            # タイマー開始判定 (1人目が完了した時点)
            done_count = sum(1 for p in room.players.values() if p.is_turn_done)
            if done_count == 1:
                room.timer_active = True
                room.turn_deadline = time.time() + (3 * 60) # 3分後
                await sio.emit('timer_start', {'deadline': room.turn_deadline}, room=room_id)

            # 他島への干渉アクション（攻撃など）があればここで処理し、
            # 対象プレイヤーの「着弾キュー」に入れる処理が必要
            # (今回は簡易実装として、クライアントから送られた「他島への行動」をブロードキャストする)
            outgoing_actions = data.get('outgoingActions', [])
            if outgoing_actions:
                # 全員にアクションをばら撒く（クライアント側で自分宛てか判断させる）
                await sio.emit('receive_external_actions', {'actions': outgoing_actions, 'from': player.island_name}, room=room_id)

            await sio.emit('system_message', {'message': f"{player.island_name}がターンを終了しました ({done_count}/{len(room.players)})"}, room=room_id)
            await check_turn_progression(room_id)

@sio.event
async def disconnect(sid):
    for room_id in list(rooms.keys()):
        if sid in rooms[room_id].players:
            player_name = rooms[room_id].players[sid].island_name
            del rooms[room_id].players[sid]
            await sio.emit('system_message', {'message': f"{player_name}が退出しました"}, room=room_id)
            if len(rooms[room_id].players) == 0:
                del rooms[room_id]
            break
    print(f"Client disconnected: {sid}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
