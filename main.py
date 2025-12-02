import sqlite3
import json
import hashlib
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Dict, List, Optional

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

# --- BASE DE DATOS ---
def init_db():
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    # Mensajes
    c.execute('''CREATE TABLE IF NOT EXISTS mensajes
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, recipient TEXT, message TEXT, timestamp TEXT, is_group INTEGER)''')
    # Usuarios
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios
                 (username TEXT PRIMARY KEY, password_hash TEXT, avatar TEXT, about TEXT)''')
    # Grupos
    c.execute('''CREATE TABLE IF NOT EXISTS grupos
                 (nombre TEXT PRIMARY KEY, creador TEXT, miembros TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- FUNCIONES BASE DE DATOS ---
def encriptar(password):
    return hashlib.sha256(password.encode()).hexdigest()

def guardar_mensaje_db(sender, recipient, message, timestamp, is_group):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
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
    c.execute("SELECT username, avatar, about FROM usuarios")
    users = c.fetchall()
    conn.close()
    return [{"username": u[0], "avatar": u[1] if u[1] else "", "about": u[2] if u[2] else "¡Hola! Uso TecChat"} for u in users]

def actualizar_avatar_db(username, nueva_url):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE usuarios SET avatar = ? WHERE username = ?", (nueva_url, username))
    conn.commit()
    conn.close()

def actualizar_about_db(username, nuevo_about):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE usuarios SET about = ? WHERE username = ?", (nuevo_about, username))
    conn.commit()
    conn.close()

# --- FUNCIONES DE GRUPOS ---
def crear_grupo_db(nombre, creador, miembros_lista):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    miembros_json = json.dumps(miembros_lista)
    try:
        c.execute("INSERT INTO grupos VALUES (?, ?, ?)", (nombre, creador, miembros_json))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def obtener_info_grupo_db(nombre_grupo):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT creador, miembros FROM grupos WHERE nombre = ?", (nombre_grupo,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"nombre": nombre_grupo, "creador": row[0], "miembros": json.loads(row[1])}
    return None

def modificar_miembros_grupo_db(nombre_grupo, nueva_lista):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    miembros_json = json.dumps(nueva_lista)
    c.execute("UPDATE grupos SET miembros = ? WHERE nombre = ?", (miembros_json, nombre_grupo))
    conn.commit()
    conn.close()

def obtener_grupos_usuario(username):
    conn = sqlite3.connect('chat.db', timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT nombre, miembros FROM grupos")
    todos = c.fetchall()
    conn.close()
    mis_grupos = []
    for g in todos:
        miembros = json.loads(g[1])
        if username in miembros:
            mis_grupos.append({"nombre": g[0], "miembros": miembros})
    return mis_grupos

# --- MODELOS ---
class UserAuth(BaseModel):
    username: str
    password: str

class UserUpdate(BaseModel):
    username: str
    avatar_url: Optional[str] = None
    about: Optional[str] = None

class NewGroup(BaseModel):
    nombre: str
    creador: str
    miembros: List[str]

class GroupAction(BaseModel):
    nombre_grupo: str
    solicitante: str
    target_user: str

# --- WEBSOCKET MANAGER ---
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
        msg = json.dumps({"type": "STATUS", "online_users": online_users})
        for conn in self.active_connections.values():
            await conn.send_text(msg)

    async def send_personal_message(self, message_json: str, recipient_id: str):
        if recipient_id in self.active_connections:
            await self.active_connections[recipient_id].send_text(message_json)

    async def broadcast(self, message_json: str):
        for conn in self.active_connections.values():
            await conn.send_text(message_json)

    async def broadcast_to_group(self, message_json: str, members_list: List[str]):
        for member in members_list:
            if member in self.active_connections:
                await self.active_connections[member].send_text(message_json)

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
        raise HTTPException(status_code=400, detail="Usuario existente.")
    c.execute("INSERT INTO usuarios VALUES (?, ?, ?, ?)", (user.username, encriptar(user.password), None, "Disponible"))
    conn.commit()
    conn.close()
    return {"message": "Creado"}

@app.post("/update-avatar")
async def update_avatar(data: UserUpdate):
    actualizar_avatar_db(data.username, data.avatar_url)
    return {"message": "Avatar actualizado"}

@app.post("/update-about")
async def update_about(data: UserUpdate):
    actualizar_about_db(data.username, data.about)
    return {"message": "Estado actualizado"}

@app.post("/crear-grupo")
async def create_group(grupo: NewGroup):
    members = list(set(grupo.miembros))
    if grupo.creador not in members: members.append(grupo.creador)
    if len(members) < 1: raise HTTPException(status_code=400, detail="Faltan miembros")
    exito = crear_grupo_db(grupo.nombre, grupo.creador, members)
    if not exito: raise HTTPException(status_code=400, detail="El grupo ya existe")
    return {"message": "Grupo creado"}

@app.get("/mis-grupos/{username}")
async def get_my_groups(username: str):
    return obtener_grupos_usuario(username)

@app.get("/grupo/{nombre}")
async def get_group_info(nombre: str):
    return obtener_info_grupo_db(nombre)

@app.post("/grupo/agregar")
async def add_member(action: GroupAction):
    info = obtener_info_grupo_db(action.nombre_grupo)
    if not info: raise HTTPException(404, "Grupo no existe")
    if action.solicitante not in info["miembros"]: raise HTTPException(403, "No eres del grupo")
    if action.target_user not in info["miembros"]:
        info["miembros"].append(action.target_user)
        modificar_miembros_grupo_db(action.nombre_grupo, info["miembros"])
    return {"message": "Agregado"}

@app.post("/grupo/expulsar")
async def kick_member(action: GroupAction):
    info = obtener_info_grupo_db(action.nombre_grupo)
    if not info: raise HTTPException(404, "Grupo no existe")
    if info["creador"] != action.solicitante: raise HTTPException(403, "Solo el creador puede expulsar")
    if action.target_user in info["miembros"]:
        info["miembros"].remove(action.target_user)
        modificar_miembros_grupo_db(action.nombre_grupo, info["miembros"])
    return {"message": "Expulsado"}

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
        # --- AQUÍ ESTÁ EL CAMBIO CLAVE: HORA UTC (Z) ---
        now = datetime.utcnow().isoformat() + "Z"
        
        sys_msg = json.dumps({"type": "CHAT", "sender": "Sistema", "recipient": "Todos", "message": f"{client_id} se ha unido", "timestamp": now, "is_group": False})
        await manager.broadcast(sys_msg)
        
        while True:
            raw_data = await websocket.receive_text()
            data_json = json.loads(raw_data)
            tipo = data_json.get("action", "message") 

            if tipo == "delete":
                msg_id = data_json["id"]
                if borrar_mensaje_db(msg_id, client_id):
                    await manager.broadcast(json.dumps({"type": "DELETE", "id": msg_id}))
            
            else:
                recipient = data_json["recipient"]
                message = data_json["message"]
                is_group = data_json.get("is_group", False)
                
                # --- AQUÍ ESTÁ EL CAMBIO CLAVE: HORA UTC (Z) ---
                hora_actual = datetime.utcnow().isoformat() + "Z"
                
                nuevo_id = guardar_mensaje_db(client_id, recipient, message, hora_actual, is_group)
                
                resp = json.dumps({
                    "type": "CHAT", "id": nuevo_id, "sender": client_id,
                    "recipient": recipient, "message": message, "timestamp": hora_actual, "is_group": is_group
                })
                
                if recipient == "Chat General":
                    await manager.broadcast(resp)
                elif is_group:
                    info_grupo = obtener_info_grupo_db(recipient)
                    if info_grupo:
                        await manager.broadcast_to_group(resp, info_grupo["miembros"])
                else:
                    await manager.send_personal_message(resp, recipient)
                    await manager.send_personal_message(resp, client_id)
            
    except WebSocketDisconnect:
        manager.disconnect(client_id)
        await manager.broadcast_online_list()