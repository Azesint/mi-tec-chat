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
    # Tabla Mensajes
    c.execute('''CREATE TABLE IF NOT EXISTS mensajes
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, recipient TEXT, message TEXT, timestamp TEXT, is_group INTEGER)''')
    # Tabla Usuarios
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios
                 (username TEXT PRIMARY KEY, password_hash TEXT, avatar TEXT)''')
    # NUEVA: Tabla Grupos (nombre, creador, lista_miembros_json)
    c.execute('''CREATE TABLE IF NOT EXISTS grupos
                 (nombre TEXT PRIMARY KEY, creador TEXT, miembros TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- FUNCIONES ---
def encriptar(password):
    return hashlib.sha256(password.encode()).hexdigest()

def guardar_mensaje_db(sender, recipient, message, timestamp, is_group):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    # is_group: 1 si es grupo, 0 si es privado
    c.execute("INSERT INTO mensajes (sender, recipient, message, timestamp, is_group) VALUES (?, ?, ?, ?, ?)", 
              (sender, recipient, message, timestamp, 1 if is_group else 0))
    conn.commit()
    nuevo_id = c.lastrowid
    conn.close()
    return nuevo_id

def borrar_mensaje_db(msg_id, sender):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("DELETE FROM mensajes WHERE id = ? AND sender = ?", (msg_id, sender))
    borrados = c.rowcount
    conn.commit()
    conn.close()
    return borrados > 0

def obtener_mensajes_db():
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT id, sender, recipient, message, timestamp, is_group FROM mensajes")
    mensajes = c.fetchall()
    conn.close()
    return [{"id": m[0], "sender": m[1], "recipient": m[2], "message": m[3], "timestamp": m[4], "is_group": bool(m[5])} for m in mensajes]

def obtener_usuarios_db():
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT username, avatar FROM usuarios")
    users = c.fetchall()
    conn.close()
    return [{"username": u[0], "avatar": u[1] if u[1] else ""} for u in users]

def actualizar_avatar_db(username, nueva_url):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE usuarios SET avatar = ? WHERE username = ?", (nueva_url, username))
    conn.commit()
    conn.close()

# --- NUEVAS FUNCIONES DE GRUPOS ---
def crear_grupo_db(nombre, creador, miembros_lista):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    # Guardamos la lista de miembros como Texto JSON (ej: '["Juan", "Pedro"]')
    miembros_json = json.dumps(miembros_lista)
    try:
        c.execute("INSERT INTO grupos VALUES (?, ?, ?)", (nombre, creador, miembros_json))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False # Ya existe el grupo
    finally:
        conn.close()

def obtener_grupos_usuario(username):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT nombre, miembros FROM grupos")
    todos_los_grupos = c.fetchall()
    conn.close()
    
    mis_grupos = []
    for grupo in todos_los_grupos:
        nombre = grupo[0]
        miembros = json.loads(grupo[1]) # Convertir texto a lista
        if username in miembros:
            mis_grupos.append({"nombre": nombre, "miembros": miembros})
    return mis_grupos

def obtener_miembros_grupo(nombre_grupo):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT miembros FROM grupos WHERE nombre = ?", (nombre_grupo,))
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return []

# --- MODELOS ---
class UserAuth(BaseModel):
    username: str
    password: str

class UserAvatarUpdate(BaseModel):
    username: str
    avatar_url: str

class NewGroup(BaseModel):
    nombre: str
    creador: str
    miembros: List[str]

# --- CONEXIÓN ---
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

    # Enviar a UNO (Privado)
    async def send_personal_message(self, message_json: str, recipient_id: str):
        if recipient_id in self.active_connections:
            websocket = self.active_connections[recipient_id]
            await websocket.send_text(message_json)

    # Enviar a TODOS (Chat General)
    async def broadcast(self, message_json: str):
        for connection in self.active_connections.values():
            await connection.send_text(message_json)

    # Enviar a GRUPO (Lista de usuarios)
    async def broadcast_to_group(self, message_json: str, members_list: List[str]):
        for member in members_list:
            if member in self.active_connections:
                websocket = self.active_connections[member]
                await websocket.send_text(message_json)

manager = ConnectionManager()

# --- RUTAS ---

@app.get("/")
async def get():
    return RedirectResponse(url="/static/index.html")

@app.post("/login")
async def login(user: UserAuth):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
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
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
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

# RUTA CREAR GRUPO
@app.post("/crear-grupo")
async def create_group(grupo: NewGroup):
    # Validar que haya al menos 2 miembros (incluyendo el creador)
    members = list(set(grupo.miembros)) # Quitar duplicados
    if grupo.creador not in members: members.append(grupo.creador)
    
    if len(members) < 2:
        raise HTTPException(status_code=400, detail="Se necesitan al menos 2 miembros")
    
    exito = crear_grupo_db(grupo.nombre, grupo.creador, members)
    if not exito:
        raise HTTPException(status_code=400, detail="Ya existe un grupo con ese nombre")
    
    return {"message": "Grupo creado"}

@app.get("/mis-grupos/{username}")
async def get_my_groups(username: str):
    return obtener_grupos_usuario(username)

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
        sys_msg = json.dumps({"type": "CHAT", "sender": "Sistema", "recipient": "Todos", "message": f"{client_id} se ha unido", "timestamp": now, "is_group": False})
        await manager.broadcast(sys_msg)
        
        while True:
            raw_data = await websocket.receive_text()
            data_json = json.loads(raw_data)
            
            tipo_accion = data_json.get("action", "message") 

            if tipo_accion == "delete":
                msg_id = data_json["id"]
                exito = borrar_mensaje_db(msg_id, client_id)
                if exito:
                    delete_msg = json.dumps({"type": "DELETE", "id": msg_id})
                    await manager.broadcast(delete_msg)
            
            else:
                recipient = data_json["recipient"]
                message = data_json["message"]
                # Detectamos si es un grupo (lo manda el frontend)
                is_group = data_json.get("is_group", False) 
                
                hora_actual = datetime.now().strftime("%I:%M %p") 
                
                nuevo_id = guardar_mensaje_db(client_id, recipient, message, hora_actual, is_group)
                
                response_msg = json.dumps({
                    "type": "CHAT",
                    "id": nuevo_id,
                    "sender": client_id,
                    "recipient": recipient,
                    "message": message,
                    "timestamp": hora_actual,
                    "is_group": is_group
                })
                
                if recipient == "Chat General":
                    await manager.broadcast(response_msg)
                elif is_group:
                    # Lógica de Grupo: Buscar miembros y enviar a ellos
                    miembros = obtener_miembros_grupo(recipient)
                    await manager.broadcast_to_group(response_msg, miembros)
                else:
                    # Privado normal
                    await manager.send_personal_message(response_msg, recipient)
                    await manager.send_personal_message(response_msg, client_id)
            
    except WebSocketDisconnect:
        manager.disconnect(client_id)
        await manager.broadcast_online_list()