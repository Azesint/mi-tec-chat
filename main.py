import sqlite3
import json
import hashlib
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Dict, List

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

# --- BASE DE DATOS ---
def init_db():
    conn = sqlite3.connect('chat.db')
    c = conn.cursor()
    # AHORA LA TABLA TIENE UN ID ÚNICO (PRIMARY KEY AUTOINCREMENT)
    c.execute('''CREATE TABLE IF NOT EXISTS mensajes
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, recipient TEXT, message TEXT, timestamp TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios
                 (username TEXT PRIMARY KEY, password_hash TEXT, avatar TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- FUNCIONES ---
def encriptar(password):
    return hashlib.sha256(password.encode()).hexdigest()

def guardar_mensaje_db(sender, recipient, message, timestamp):
    conn = sqlite3.connect('chat.db')
    c = conn.cursor()
    c.execute("INSERT INTO mensajes (sender, recipient, message, timestamp) VALUES (?, ?, ?, ?)", 
              (sender, recipient, message, timestamp))
    conn.commit()
    # Recuperamos el ID del mensaje recién creado para enviarlo al frontend
    nuevo_id = c.lastrowid
    conn.close()
    return nuevo_id

def borrar_mensaje_db(msg_id, sender):
    conn = sqlite3.connect('chat.db')
    c = conn.cursor()
    # Solo borramos si el ID coincide Y si el que lo pide es el dueño del mensaje (sender)
    c.execute("DELETE FROM mensajes WHERE id = ? AND sender = ?", (msg_id, sender))
    borrados = c.rowcount # Nos dice cuántas filas borró (1 si éxito, 0 si falló)
    conn.commit()
    conn.close()
    return borrados > 0

def obtener_mensajes_db():
    conn = sqlite3.connect('chat.db')
    c = conn.cursor()
    # Recuperamos también el ID
    c.execute("SELECT id, sender, recipient, message, timestamp FROM mensajes")
    mensajes = c.fetchall()
    conn.close()
    return [{"id": m[0], "sender": m[1], "recipient": m[2], "message": m[3], "timestamp": m[4]} for m in mensajes]

def obtener_usuarios_db():
    conn = sqlite3.connect('chat.db')
    c = conn.cursor()
    c.execute("SELECT username, avatar FROM usuarios")
    users = c.fetchall()
    conn.close()
    return [{"username": u[0], "avatar": u[1] if u[1] else ""} for u in users]

def actualizar_avatar_db(username, nueva_url):
    conn = sqlite3.connect('chat.db')
    c = conn.cursor()
    c.execute("UPDATE usuarios SET avatar = ? WHERE username = ?", (nueva_url, username))
    conn.commit()
    conn.close()

class UserAuth(BaseModel):
    username: str
    password: str

class UserAvatarUpdate(BaseModel):
    username: str
    avatar_url: str

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        await self.broadcast_online_list()

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def broadcast_online_list(self):
        online_users = list(self.active_connections.keys())
        message = json.dumps({"type": "STATUS", "online_users": online_users})
        for connection in self.active_connections.values():
            await connection.send_text(message)

    async def send_personal_message(self, message_json: str, recipient_id: str):
        if recipient_id in self.active_connections:
            websocket = self.active_connections[recipient_id]
            await websocket.send_text(message_json)

    async def broadcast(self, message_json: str):
        for connection in self.active_connections.values():
            await connection.send_text(message_json)

manager = ConnectionManager()

# --- RUTAS ---

@app.get("/")
async def get():
    return RedirectResponse(url="/static/index.html")

@app.post("/login")
async def login(user: UserAuth):
    conn = sqlite3.connect('chat.db')
    c = conn.cursor()
    c.execute("SELECT password_hash FROM usuarios WHERE username = ?", (user.username,))
    row = c.fetchone()
    conn.close()
    if not row: raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if row[0] != encriptar(user.password): raise HTTPException(status_code=401, detail="Contraseña incorrecta.")
    return {"message": "Login exitoso"}

@app.post("/signup")
async def signup(user: UserAuth):
    if not user.password: raise HTTPException(status_code=400, detail="Contraseña obligatoria")
    conn = sqlite3.connect('chat.db')
    c = conn.cursor()
    c.execute("SELECT username FROM usuarios WHERE username = ?", (user.username,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="El usuario ya existe.")
    c.execute("INSERT INTO usuarios VALUES (?, ?, ?)", (user.username, encriptar(user.password), None))
    conn.commit()
    conn.close()
    return {"message": "Creado"}

@app.post("/update-avatar")
async def update_avatar(data: UserAvatarUpdate):
    actualizar_avatar_db(data.username, data.avatar_url)
    return {"message": "Avatar actualizado"}

@app.get("/lista-usuarios/")
async def get_users():
    return obtener_usuarios_db()

@app.get("/historial")
async def get_history():
    return obtener_mensajes_db()

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket, client_id)
    try:
        now = datetime.now().strftime("%I:%M %p")
        sys_msg = json.dumps({"type": "CHAT", "sender": "Sistema", "recipient": "Todos", "message": f"{client_id} se ha unido", "timestamp": now})
        await manager.broadcast(sys_msg)
        
        while True:
            raw_data = await websocket.receive_text()
            data_json = json.loads(raw_data)
            
            # --- DETECTAR SI ES UN MENSAJE NORMAL O UNA ORDEN DE BORRAR ---
            tipo_accion = data_json.get("action", "message") # Si no dice nada, es mensaje

            if tipo_accion == "delete":
                # LÓGICA DE BORRADO
                msg_id = data_json["id"]
                exito = borrar_mensaje_db(msg_id, client_id)
                if exito:
                    # Avisamos a TODOS que borren ese ID de su pantalla
                    delete_msg = json.dumps({"type": "DELETE", "id": msg_id})
                    await manager.broadcast(delete_msg)
            
            else:
                # LÓGICA DE MENSAJE NORMAL
                recipient = data_json["recipient"]
                message = data_json["message"]
                hora_actual = datetime.now().strftime("%I:%M %p") 
                
                # Guardamos y obtenemos el ID nuevo
                nuevo_id = guardar_mensaje_db(client_id, recipient, message, hora_actual)
                
                response_msg = json.dumps({
                    "type": "CHAT",
                    "id": nuevo_id, # Enviamos el ID al frontend
                    "sender": client_id,
                    "recipient": recipient,
                    "message": message,
                    "timestamp": hora_actual
                })
                
                if recipient == "Chat General":
                    await manager.broadcast(response_msg)
                else:
                    await manager.send_personal_message(response_msg, recipient)
                    await manager.send_personal_message(response_msg, client_id)
            
    except WebSocketDisconnect:
        manager.disconnect(client_id)
        await manager.broadcast_online_list()
        now = datetime.now().strftime("%I:%M %p")
        sys_msg = json.dumps({"type": "CHAT", "sender": "Sistema", "recipient": "Todos", "message": f"{client_id} ha salido", "timestamp": now})
        await manager.broadcast(sys_msg)