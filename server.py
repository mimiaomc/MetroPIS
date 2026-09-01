import asyncio
import glob
import json
import os
import re
import struct
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_master_loop())
    yield
    task.cancel()

app = FastAPI(title="Metro PIDS Central OCC Server", lifespan=lifespan)

# 启用 CORS 跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== 1. 动态线路配置加载引擎 (支持 lines/*.json) =====
def load_all_lines() -> Dict[int, dict]:
    lines = {}
    lines_dir = "lines"
    if not os.path.exists(lines_dir):
        os.makedirs(lines_dir, exist_ok=True)
    
    json_files = glob.glob(os.path.join(lines_dir, "*.json"))
    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
                line_id = int(data.get("line_id", 3))
                lines[line_id] = data
                print(f"🚇 [线路库] 动态加载线路配置: {data.get('name_cn', f'Line {line_id}')} (共 {len(data.get('stations', []))} 站)")
        except Exception as e:
            print(f"❌ 加载线路配置文件失败 {jf}: {e}")
            
    if not lines:
        print("⚠️ 未发现 lines/*.json 配置文件，使用内置演示线路配置")
        lines[1] = {
            "line_id": 1,
            "name_cn": "示例线路1号线",
            "name_en": "Demo Line 1",
            "color": "#0284c7",
            "stations": [
                {"id": 0, "cn": "起始站", "en": "START STATION", "short": "始发"},
                {"id": 1, "cn": "中间站", "en": "CENTRAL STATION", "short": "中间"},
                {"id": 2, "cn": "终点站", "en": "TERMINAL STATION", "short": "终点"}
            ]
        }
    return lines

LINES_REGISTRY = load_all_lines()
DEFAULT_LINE_ID = sorted(LINES_REGISTRY.keys())[0] if LINES_REGISTRY else 1
ACTIVE_LINE = LINES_REGISTRY[DEFAULT_LINE_ID]
STATION_MAP = {s["id"]: s for s in ACTIVE_LINE.get("stations", [])}

# ===== 2. 视频动态扫描与时长解析 (支持 Videos/Video*.mp4 及所有通配符) =====
def get_mp4_duration(file_path: str) -> Optional[float]:
    """纯 Python 解析 MP4 文件的 mvhd box 提取精确时长（秒）"""
    try:
        with open(file_path, 'rb') as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                size, name = struct.unpack('>I4s', header)
                if name == b'moov':
                    moov_data = f.read(size - 8)
                    idx = 0
                    while idx < len(moov_data):
                        box_size, box_name = struct.unpack('>I4s', moov_data[idx:idx+8])
                        if box_name == b'mvhd':
                            version = moov_data[idx+8]
                            if version == 0:
                                timescale, duration = struct.unpack('>II', moov_data[idx+20:idx+28])
                            else:
                                timescale, duration = struct.unpack('>IQ', moov_data[idx+28:idx+40])
                            return round(duration / timescale, 2)
                        idx += box_size
                elif size == 1:
                    large_size = struct.unpack('>Q', f.read(8))[0]
                    f.seek(large_size - 16, 1)
                else:
                    f.seek(size - 8, 1)
    except Exception as e:
        print(f"解析视频时长失败 {file_path}: {e}")
    return 60.0

def scan_playlist() -> List[dict]:
    """使用通配符动态扫描 Videos/ 目录下的所有 .mp4 文件并自然排序"""
    pattern = os.path.join("Videos", "*.mp4")
    files = glob.glob(pattern)
    
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
    
    files.sort(key=natural_sort_key)
    
    playlist = []
    for fpath in files:
        dur = get_mp4_duration(fpath) or 60.0
        norm_path = fpath.replace("\\", "/")
        playlist.append({
            "file": norm_path,
            "name": os.path.basename(fpath),
            "duration": dur
        })
    print(f"🎬 [视频库] 动态扫描发现 {len(playlist)} 部视频: {[p['name'] + ' (' + str(p['duration']) + 's)' for p in playlist]}")
    return playlist

# 全线统一视频母钟播控状态
initial_playlist = scan_playlist()
video_state = {
    "playlist": initial_playlist,
    "current_index": 0,
    "current_video": initial_playlist[0]["file"] if initial_playlist else None,
    "duration": initial_playlist[0]["duration"] if initial_playlist else 60.0,
    "start_time": time.time(),
    "auto_loop": True,
    "is_black": False
}

# ===== 3. 列车运行图与调度状态仓库 (ATS Telemetry & Dispatch Storage) =====
def init_all_stations_dispatch(line_config: dict):
    st_dict = {}
    stations = line_config.get("stations", [])
    routing = line_config.get("routing_pattern", {})
    full_term = routing.get("full_turn_terminal", 25)
    short_term = routing.get("short_turn_terminal", 20)
    start_term = routing.get("start_terminal", 0)

    for s in stations:
        st_id = s["id"]
        st_dict[st_id] = {
            1: {
                "trip1": {"dest": short_term if st_id < short_term else full_term, "countdown": 3, "status": "COUNTDOWN"},
                "trip2": {"dest": full_term, "countdown": 6, "status": "NORMAL"},
                "ticker": f"欢迎乘坐{line_config.get('name_cn', '城市轨道交通')}！",
            },
            2: {
                "trip1": {"dest": start_term, "countdown": 4, "status": "COUNTDOWN"},
                "trip2": {"dest": start_term, "countdown": 8, "status": "NORMAL"},
                "ticker": f"欢迎乘坐{line_config.get('name_cn', '城市轨道交通')}！",
            }
        }
    return st_dict

LINE_DISPATCH: Dict[int, dict] = {lid: init_all_stations_dispatch(cfg) for lid, cfg in LINES_REGISTRY.items()}
LINE_TRAINS: Dict[int, list] = {lid: [] for lid in LINES_REGISTRY.keys()}
MANUAL_OVERRIDES: Dict[tuple, dict] = {}
LAST_ATS_TIMESTAMP: float = 0.0

# 核心全局调度状态
dispatch_state = {
    "signaling_mode": "ATS_ONLINE",
    "active_line_id": DEFAULT_LINE_ID,
    "active_trains": [],
    "stations": LINE_DISPATCH.get(DEFAULT_LINE_ID, {}),
    "global_ticker": f"欢迎乘坐{ACTIVE_LINE.get('name_cn', '城市轨道交通')}！请先下后上，注意站台间隙。",
    "emergency": {},
    "live_stream": None
}

# ===== 4. WebSocket 连接管理器 =====
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[WebSocket, dict] = {}

    async def connect(self, websocket: WebSocket, screen_info: dict):
        await websocket.accept()
        self.active_connections[websocket] = screen_info
        print(f"✅ 屏幕已连接: [{screen_info.get('device_id')}] - 当前在线屏幕数: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            info = self.active_connections.pop(websocket)
            print(f"❌ 屏幕断开连接: [{info.get('device_id')}] - 剩余在线屏幕数: {len(self.active_connections)}")

    async def broadcast_state(self):
        """向所有连接的屏幕推送最新状态"""
        for ws, info in list(self.active_connections.items()):
            try:
                payload = self.build_screen_payload(info)
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                print(f"推送消息失败: {e}")

    def build_screen_payload(self, screen_info: dict) -> dict:
        line_id = screen_info.get("line", DEFAULT_LINE_ID)
        line_cfg = LINES_REGISTRY.get(line_id, ACTIVE_LINE)
        line_st_map = {s["id"]: s for s in line_cfg.get("stations", [])}

        st_id = screen_info.get("station", 0)
        pf_id = screen_info.get("platform", 1)

        st_meta = line_st_map.get(st_id, {"cn": f"站号{st_id}", "en": f"STATION {st_id}"})
        
        line_stations = LINE_DISPATCH.get(line_id, {})
        routing = line_cfg.get("routing_pattern", {})
        default_term = routing.get("full_turn_terminal", 25) if pf_id == 1 else routing.get("start_terminal", 0)

        station_data = line_stations.get(st_id, {}).get(pf_id, {
            "trip1": {"dest": default_term, "countdown": 3, "status": "COUNTDOWN"},
            "trip2": {"dest": default_term, "countdown": 6, "status": "NORMAL"},
            "ticker": dispatch_state["global_ticker"]
        })

        emergency_data = dispatch_state["emergency"].get(st_id, None)

        # 计算当前视频母钟进度
        elapsed = max(0.0, round(time.time() - video_state["start_time"], 1))
        remaining = max(0.0, round(video_state["duration"] - elapsed, 1))

        t1_dest = station_data["trip1"]["dest"]
        t2_dest = station_data["trip2"]["dest"]

        t1_meta = line_st_map.get(t1_dest, {"cn": "终点站", "en": "TERMINAL"})
        t2_meta = line_st_map.get(t2_dest, {"cn": "终点站", "en": "TERMINAL"})

        t1_status = station_data["trip1"].get("status", "COUNTDOWN")

        return {
            "type": "UPDATE",
            "server_time": int(time.time() * 1000),
            "screen": {
                "line": line_id,
                "line_name": line_cfg.get("name_cn", "城市轨道交通"),
                "line_color": line_cfg.get("color", "#e91e63"),
                "station_id": st_id,
                "station_cn": st_meta["cn"],
                "station_en": st_meta["en"],
                "platform": pf_id,
                "screen_num": screen_info.get("screen", 1),
                "device_id": screen_info.get("device_id", f"{line_id:02d}-{st_id:02d}-{pf_id}-{screen_info.get('screen', 1):02d}"),
                "watermark": f"{line_cfg.get('name_cn', '')} · {st_meta['cn']}站 · {pf_id}号站台 [{line_id:02d}-{st_id:02d}-{pf_id}-{screen_info.get('screen', 1):02d}]"
            },
            "trip1": {
                "dest_id": t1_dest,
                "dest_cn": t1_meta["cn"],
                "dest_en": t1_meta["en"],
                "countdown": station_data["trip1"]["countdown"],
                "status": t1_status
            },
            "trip2": {
                "dest_id": t2_dest,
                "dest_cn": t2_meta["cn"],
                "dest_en": t2_meta["en"],
                "countdown": station_data["trip2"]["countdown"],
                "status": station_data["trip2"]["status"]
            },
            "ticker": station_data.get("ticker", dispatch_state["global_ticker"]),
            "emergency": emergency_data,
            "live_stream": dispatch_state["live_stream"],
            "video": {
                "current_video": None if video_state["is_black"] else video_state["current_video"],
                "name": None if video_state["is_black"] else (os.path.basename(video_state["current_video"]) if video_state["current_video"] else None),
                "duration": video_state["duration"],
                "elapsed": elapsed,
                "remaining": remaining,
                "is_black": video_state["is_black"],
                "auto_loop": video_state["auto_loop"],
                "playlist": video_state["playlist"],
                "current_index": video_state["current_index"]
            }
        }

manager = ConnectionManager()

# ===== 4. WebSocket 终端端点 =====
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # 解析连接参数
    query_params = dict(websocket.query_params)
    line = int(query_params.get("line", DEFAULT_LINE_ID))
    station = int(query_params.get("station", 0))
    platform = int(query_params.get("platform", 1))
    screen = int(query_params.get("screen", 1))
    device_id = f"{line:02d}-{station:02d}-{platform}-{screen:02d}"

    screen_info = {
        "line": line,
        "station": station,
        "platform": platform,
        "screen": screen,
        "device_id": device_id
    }

    await manager.connect(websocket, screen_info)
    # 立即下发初始状态
    initial_payload = manager.build_screen_payload(screen_info)
    line_cfg = LINES_REGISTRY.get(line, ACTIVE_LINE)
    initial_payload["topology"] = line_cfg.get("stations", [])
    await websocket.send_text(json.dumps(initial_payload, ensure_ascii=False))

    try:
        while True:
            msg_text = await websocket.receive_text()
            try:
                data = json.loads(msg_text)
                # 处理心跳保活包（保持 Cloudflare Tunnel 长连接不断线）
                if data.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "server_time": int(time.time() * 1000)}))
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket 异常: {e}")
        manager.disconnect(websocket)

# ===== 5. 后台核心守护协程（视频母钟中央轮播 + WebSocket 广播） =====
async def background_master_loop():
    """全线母钟总调度守护：每秒轮巡视频母钟进度并向所有在线屏幕广播状态"""
    while True:
        await asyncio.sleep(1)

        # 1. 视频母钟自动轮播切片
        if video_state["playlist"] and video_state["auto_loop"] and not video_state["is_black"]:
            elapsed = time.time() - video_state["start_time"]
            curr_dur = video_state["duration"]
            if elapsed >= curr_dur:
                video_state["current_index"] = (video_state["current_index"] + 1) % len(video_state["playlist"])
                next_item = video_state["playlist"][video_state["current_index"]]
                video_state["current_video"] = next_item["file"]
                video_state["duration"] = next_item["duration"]
                video_state["start_time"] = time.time()
                print(f"🎬 [视频母钟] 自动轮播切片: {next_item['name']} (时长: {next_item['duration']}s)")

        # 2. 每秒向所有在线屏幕广播最新的母钟与进站状态
        if manager.active_connections:
            await manager.broadcast_state()

# ===== 6. ATS 信号遥测接收接口 (接收外部 simulator.py 或真实信号机数据) =====
@app.post("/api/ats/update")
async def ats_telemetry_update(req: Request):
    """
    接收来自独立 ATS 信号发生器 (simulator.py) 或外部信号系统的行车与站台预测数据
    """
    global LAST_ATS_TIMESTAMP
    LAST_ATS_TIMESTAMP = time.time()
    data = await req.json()
    line_id = int(data.get("line_id", DEFAULT_LINE_ID))
    trains = data.get("trains", [])
    stations = data.get("stations", {})
    
    LINE_TRAINS[line_id] = trains
    
    if line_id not in LINE_DISPATCH:
        LINE_DISPATCH[line_id] = {}
        
    for st_id_str, pfs in stations.items():
        st_id = int(st_id_str)
        if st_id not in LINE_DISPATCH[line_id]:
            LINE_DISPATCH[line_id][st_id] = {1: {"trip1": {}, "trip2": {}}, 2: {"trip1": {}, "trip2": {}}}
        for pf_id_str, trips in pfs.items():
            pf_id = int(pf_id_str)
            if pf_id not in LINE_DISPATCH[line_id][st_id]:
                LINE_DISPATCH[line_id][st_id][pf_id] = {"trip1": {}, "trip2": {}}
                
            curr_st = LINE_DISPATCH[line_id][st_id][pf_id]
            override_key = (line_id, st_id, pf_id)
            
            # 检查是否有 OCC 手动覆盖本趟车次
            if override_key in MANUAL_OVERRIDES:
                ov = MANUAL_OVERRIDES[override_key]
                # 如果是 NONSTOP，当车次过站后自动恢复
                if ov.get("status") == "NONSTOP":
                    auto_status = trips.get("trip1", {}).get("status")
                    if auto_status == "COUNTDOWN" and ov.get("_passed"):
                        MANUAL_OVERRIDES.pop(override_key, None)
                        curr_st["trip1"] = trips.get("trip1", {})
                        curr_st["trip2"] = trips.get("trip2", {})
                    else:
                        if auto_status in ("ARRIVED", "ARRIVING"):
                            ov["_passed"] = True
                        curr_st["trip1"]["status"] = "NONSTOP"
                        curr_st["trip1"]["countdown"] = 0
                        curr_st["trip1"]["dest"] = trips.get("trip1", {}).get("dest", 0)
                        curr_st["trip2"] = trips.get("trip2", {})
                elif ov.get("status") in ("OUT_OF_SERVICE", "OUTOFSERVICE"):
                    # 退出服务：本趟与下一趟全部退服，保留正常终点站信息
                    curr_st["trip1"]["status"] = "OUT_OF_SERVICE"
                    curr_st["trip1"]["countdown"] = 0
                    curr_st["trip1"]["dest"] = trips.get("trip1", {}).get("dest", curr_st["trip1"].get("dest", 0))
                    curr_st["trip2"]["status"] = "OUT_OF_SERVICE"
                    curr_st["trip2"]["countdown"] = 0
                    curr_st["trip2"]["dest"] = trips.get("trip2", {}).get("dest", curr_st["trip2"].get("dest", 0))
                else:
                    # 保持手动设置的状态，直接忽略 simulator 的 trip1 数据！
                    curr_st["trip1"]["status"] = ov.get("status", "COUNTDOWN")
                    curr_st["trip1"]["countdown"] = ov.get("countdown", 3)
                    curr_st["trip1"]["dest"] = ov.get("dest") or trips.get("trip1", {}).get("dest", 0)
                    curr_st["trip2"] = trips.get("trip2", {})
            else:
                curr_st["trip1"] = trips.get("trip1", {})
                curr_st["trip2"] = trips.get("trip2", {})
    
    # 同步当前活跃线路给 OCC 控制台
    if line_id == dispatch_state.get("active_line_id"):
        dispatch_state["active_trains"] = trains
        dispatch_state["stations"] = LINE_DISPATCH.get(line_id, {})
        
    return {"status": "ok", "received_line": line_id, "trains_count": len(trains)}

# ===== 7. OCC 调度管理控制台 API =====
@app.post("/api/dispatch")
async def update_dispatch(req: Request):
    """OCC 调度中心指令下发"""
    data = await req.json()
    action = data.get("action")
    active_lid = dispatch_state.get("active_line_id", DEFAULT_LINE_ID)

    if action == "UPDATE_TRIP":
        st_id = int(data.get("station", 0))
        pf_id = int(data.get("platform", 1))
        
        # 写入当前活跃线路的调度存储
        if active_lid in LINE_DISPATCH and st_id in LINE_DISPATCH[active_lid] and pf_id in LINE_DISPATCH[active_lid][st_id]:
            target_st = LINE_DISPATCH[active_lid][st_id][pf_id]
            if "trip1_status" in data:
                target_st["trip1"]["status"] = data["trip1_status"]
                target_st["trip1"]["_passed"] = False
            if "trip1_countdown" in data and data["trip1_countdown"] != "":
                target_st["trip1"]["countdown"] = int(data["trip1_countdown"])
            if "trip1_dest" in data:
                target_st["trip1"]["dest"] = int(data["trip1_dest"])
                
            # 记录人工覆盖，后续 simulator.py 的更新会自动忽略本站台本趟车次！
            override_key = (active_lid, st_id, pf_id)
            MANUAL_OVERRIDES[override_key] = {
                "status": target_st["trip1"].get("status", "COUNTDOWN"),
                "countdown": target_st["trip1"].get("countdown", 3),
                "dest": target_st["trip1"].get("dest", 0),
                "_passed": False
            }

    elif action == "RESTORE_AUTO":
        st_id = int(data.get("station", 0))
        pf_id = int(data.get("platform", 1))
        override_key = (active_lid, st_id, pf_id)
        MANUAL_OVERRIDES.pop(override_key, None)

    elif action == "LINE_OUT_OF_SERVICE":
        line_cfg = LINES_REGISTRY.get(active_lid, ACTIVE_LINE)
        for s in line_cfg.get("stations", []):
            st_id = s["id"]
            for pf_id in (1, 2):
                if active_lid in LINE_DISPATCH and st_id in LINE_DISPATCH[active_lid]:
                    curr_t1_dest = LINE_DISPATCH[active_lid][st_id][pf_id]["trip1"].get("dest", 0)
                    curr_t2_dest = LINE_DISPATCH[active_lid][st_id][pf_id]["trip2"].get("dest", 0)
                    LINE_DISPATCH[active_lid][st_id][pf_id]["trip1"]["status"] = "OUT_OF_SERVICE"
                    LINE_DISPATCH[active_lid][st_id][pf_id]["trip1"]["countdown"] = 0
                    LINE_DISPATCH[active_lid][st_id][pf_id]["trip2"]["status"] = "OUT_OF_SERVICE"
                    LINE_DISPATCH[active_lid][st_id][pf_id]["trip2"]["countdown"] = 0
                    MANUAL_OVERRIDES[(active_lid, st_id, pf_id)] = {
                        "status": "OUT_OF_SERVICE", "countdown": 0, "dest": curr_t1_dest, "_passed": False
                    }
        print(f"⛔ [OCC调度] 线路 [{line_cfg.get('name_cn', f'Line {active_lid}')}] 已设为全线退出服务！")
        await manager.broadcast_state()

    elif action == "RESTORE_ALL_AUTO":
        to_del = [k for k in MANUAL_OVERRIDES.keys() if k[0] == active_lid]
        for k in to_del:
            MANUAL_OVERRIDES.pop(k, None)
        print(f"🟢 [OCC调度] 线路 [{active_lid}] 已解除人工锁定，恢复全线自动行车时刻表！")
        await manager.broadcast_state()

    elif action == "SET_TICKER":
        dispatch_state["global_ticker"] = data.get("text", dispatch_state["global_ticker"])

    elif action == "TRIGGER_EMERGENCY":
        st_id = int(data.get("station", 0))
        active = bool(data.get("active", True))
        if active:
            dispatch_state["emergency"][st_id] = {
                "type": data.get("type", "EVACUATION"),
                "message": data.get("message", "车站发生紧急情况，请听从工作人员指挥有序疏散！"),
                "active": True
            }
        else:
            dispatch_state["emergency"].pop(st_id, None)

    elif action == "SWITCH_VIDEO":
        idx = int(data.get("index", 0))
        if 0 <= idx < len(video_state["playlist"]):
            video_state["current_index"] = idx
            video_state["current_video"] = video_state["playlist"][idx]["file"]
            video_state["duration"] = video_state["playlist"][idx]["duration"]
            video_state["start_time"] = time.time()
            video_state["is_black"] = False
            print(f"🎬 [视频调度] 手动切播: {video_state['playlist'][idx]['name']}")

    elif action == "SEEK_VIDEO":
        target_sec = float(data.get("time", 0.0))
        target_sec = max(0.0, min(target_sec, video_state["duration"]))
        video_state["start_time"] = time.time() - target_sec
        print(f"🎬 [视频调度] 精准跳转至: {target_sec:.1f}s")

    elif action == "NEXT_VIDEO":
        if video_state["playlist"]:
            video_state["current_index"] = (video_state["current_index"] + 1) % len(video_state["playlist"])
            next_item = video_state["playlist"][video_state["current_index"]]
            video_state["current_video"] = next_item["file"]
            video_state["duration"] = next_item["duration"]
            video_state["start_time"] = time.time()
            video_state["is_black"] = False
            print(f"🎬 [视频调度] 手动切下一部: {next_item['name']}")

    elif action == "BLACK_SCREEN":
        video_state["is_black"] = bool(data.get("active", True))
        print(f"🎬 [视频调度] 全线黑屏状态: {video_state['is_black']}")

    elif action == "TOGGLE_AUTO_LOOP":
        video_state["auto_loop"] = bool(data.get("active", not video_state["auto_loop"]))
        print(f"🎬 [视频调度] 自动轮播状态: {video_state['auto_loop']}")

    elif action == "RESCAN_VIDEOS":
        video_state["playlist"] = scan_playlist()
        if video_state["current_index"] >= len(video_state["playlist"]):
            video_state["current_index"] = 0
            if video_state["playlist"]:
                video_state["current_video"] = video_state["playlist"][0]["file"]
                video_state["duration"] = video_state["playlist"][0]["duration"]

    await manager.broadcast_state()
    return {"status": "ok", "state": dispatch_state}

@app.get("/api/video_status")
async def get_video_status():
    elapsed = max(0.0, round(time.time() - video_state["start_time"], 1))
    remaining = max(0.0, round(video_state["duration"] - elapsed, 1))

    online_screens = []
    for ws, info in list(manager.active_connections.items()):
        lid = info.get("line", DEFAULT_LINE_ID)
        lcfg = LINES_REGISTRY.get(lid, ACTIVE_LINE)
        lst_map = {s["id"]: s for s in lcfg.get("stations", [])}
        st_meta = lst_map.get(info.get("station", 0), {"cn": f"站号{info.get('station', 0)}"})
        online_screens.append({
            "device_id": info.get("device_id"),
            "line": lid,
            "line_name": lcfg.get("name_cn", f"Line {lid}"),
            "station": info.get("station"),
            "station_cn": st_meta["cn"],
            "platform": info.get("platform"),
            "screen": info.get("screen")
        })

    is_ats_online = (time.time() - LAST_ATS_TIMESTAMP) < 4.0 if LAST_ATS_TIMESTAMP > 0 else False
    return {
        "current_video": video_state["current_video"],
        "name": os.path.basename(video_state["current_video"]) if video_state["current_video"] else None,
        "duration": video_state["duration"],
        "elapsed": elapsed,
        "remaining": remaining,
        "is_black": video_state["is_black"],
        "auto_loop": video_state["auto_loop"],
        "playlist": video_state["playlist"],
        "current_index": video_state["current_index"],
        "online_count": len(manager.active_connections),
        "online_screens": online_screens,
        "is_ats_online": is_ats_online,
        "last_ats_time": LAST_ATS_TIMESTAMP,
        "signaling_mode": "ATS_ONLINE" if is_ats_online else "MANUAL",
        "active_trains": LINE_TRAINS.get(dispatch_state.get("active_line_id"), []),
        "stations": dispatch_state["stations"]
    }

# ===== 8. 内置 OCC 调度可视化控制台页面 (/control) =====
@app.get("/control", response_class=HTMLResponse)
async def control_panel(line: Optional[int] = Query(None)):
    global ACTIVE_LINE, dispatch_state, STATION_MAP
    
    # 动态热扫描线路库
    loaded_lines = load_all_lines()
    if loaded_lines:
        LINES_REGISTRY.clear()
        LINES_REGISTRY.update(loaded_lines)
    
    if line is not None and line in LINES_REGISTRY:
        selected_line = LINES_REGISTRY[line]
        if selected_line["line_id"] != dispatch_state.get("active_line_id"):
            ACTIVE_LINE = selected_line
            STATION_MAP = {s["id"]: s for s in ACTIVE_LINE.get("stations", [])}
            dispatch_state["active_line_id"] = line
            dispatch_state["stations"] = LINE_DISPATCH.get(line, init_all_stations_dispatch(ACTIVE_LINE))
            dispatch_state["global_ticker"] = f"欢迎乘坐{ACTIVE_LINE.get('name_cn', '城市轨道交通')}！请先下后上，注意站台间隙。"
            print(f"🎛️ [OCC切换线路] 调度中心已切换至: {ACTIVE_LINE.get('name_cn', f'Line {line}')}")
    else:
        selected_line = LINES_REGISTRY.get(dispatch_state.get("active_line_id"), ACTIVE_LINE)

    active_st_list = selected_line.get("stations", [])
    station_options = "".join([f'<option value="{s["id"]}">{s["id"]:02d} - {s["cn"]} ({s["en"]})</option>' for s in active_st_list])
    line_name = selected_line.get("name_cn", "城市轨道交通")
    line_json_str = json.dumps(selected_line, ensure_ascii=False)
    
    line_options = "".join([f'<option value="{lid}" {"selected" if lid == selected_line["line_id"] else ""}>{lcfg.get("name_cn", f"Line {lid}")}</option>' for lid, lcfg in LINES_REGISTRY.items()])
    
    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>{line_name} · PIDS OCC 调度控制中心</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
    header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 12px; flex-wrap: wrap; gap: 10px; }}
    h1 {{ color: #38bdf8; display: flex; align-items: center; gap: 10px; font-size: 22px; margin: 0; }}
    .mode-bar {{ display: flex; align-items: center; gap: 8px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; margin-top: 20px; }}
    .panel {{ background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }}
    .panel h2 {{ font-size: 17px; margin-top: 0; color: #f1f5f9; border-bottom: 1px solid #475569; padding-bottom: 8px; }}
    .form-group {{ margin-bottom: 14px; }}
    label {{ display: block; font-size: 13px; color: #94a3b8; margin-bottom: 6px; font-weight: 500; }}
    input, select, textarea {{ width: 100%; box-sizing: border-box; background: #0f172a; border: 1px solid #475569; border-radius: 6px; color: #fff; padding: 8px 12px; font-size: 14px; }}
    button {{ background: #0284c7; color: #fff; border: none; padding: 9px 14px; border-radius: 6px; font-weight: 600; cursor: pointer; transition: 0.2s; width: 100%; font-size: 13px; margin-top: 6px; }}
    button:hover {{ background: #0369a1; }}
    button.danger {{ background: #e11d48; }}
    button.danger:hover {{ background: #be123c; }}
    button.success {{ background: #059669; }}
    button.success:hover {{ background: #047857; }}
    button.secondary {{ background: #475569; }}
    button.secondary:hover {{ background: #334155; }}
    .badge {{ display: inline-block; background: #0369a1; color: #fff; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }}
    .badge-warn {{ background: #d97706; }}
    .badge-danger {{ background: #e11d48; }}
    .badge-success {{ background: #059669; }}
    .badge-cbtc {{ background: #6366f1; }}
    .status-box {{ background: #0f172a; padding: 12px; border-radius: 8px; font-size: 13px; line-height: 1.6; color: #cbd5e1; border-left: 4px solid #38bdf8; margin-bottom: 12px; }}
    .progress-bar-bg {{ background: #334155; height: 8px; border-radius: 4px; overflow: hidden; margin: 8px 0; }}
    .progress-bar-fill {{ background: #38bdf8; height: 100%; width: 0%; transition: width 0.5s linear; }}
    .btn-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
    .screens-tag-box {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; max-height: 80px; overflow-y: auto; }}
    .screen-tag {{ background: #334155; color: #38bdf8; padding: 2px 6px; border-radius: 4px; font-size: 11px; }}
  </style>
</head>
<body>
  <!-- 浮动提示容器（无弹窗干扰） -->
  <div id="toast-container" style="position: fixed; top: 20px; right: 20px; z-index: 9999; display: flex; flex-direction: column; gap: 10px; pointer-events: none;"></div>

  <header>
    <div style="display: flex; align-items: center; gap: 15px; flex-wrap: wrap;">
      <h1>🚇 {line_name} · PIDS OCC 调度控制中心</h1>
      <div style="display: flex; align-items: center; gap: 6px;">
        <label style="margin-bottom: 0; color: #94a3b8; font-size: 13px;">选择线路：</label>
        <select id="line_select_header" onchange="onLineChange(this.value)" style="width: auto; background: #0f172a; border: 1px solid #0284c7; color: #38bdf8; font-weight: bold; border-radius: 6px; padding: 4px 10px; cursor: pointer;">
          {line_options}
        </select>
      </div>
    </div>
    <div class="mode-bar">
      <span class="badge" id="online-count-badge">在线屏幕: 0</span>
      <span class="badge badge-success" id="mode-badge">🟢 ATS 信号在线接入</span>
    </div>
  </header>

  <!-- 当前在线屏幕监视条 -->
  <div class="panel" style="margin-top: 15px; padding: 12px 20px;">
    <div style="font-size: 13px; color: #94a3b8; display: flex; justify-content: space-between; align-items: center;">
      <span><strong>📡 在线屏幕终端清单：</strong><span id="screens-summary">暂无屏幕接入</span></span>
      <button class="secondary" style="width: auto; margin-top: 0; padding: 3px 8px; font-size: 11px;" onclick="pollVideoStatus()">🔄 刷新状态</button>
    </div>
    <div class="screens-tag-box" id="screens-tag-list"></div>
  </div>

  <div class="panel" style="margin-top: 15px; padding: 12px 20px;">
    <h2>🚆 上下行运行图 (轨道交通区间闭塞与折返占线图)</h2>
    <div style="overflow-x: auto; width: 100%;">
      <div id="train-graph-container" style="position: relative; background: #0b1329; border-radius: 8px; border: 1px solid #334155; min-width: 1060px; height: 180px; padding: 5px;">
        <svg id="track-svg" viewBox="0 0 1060 170" width="100%" height="170px" style="display: block;">
          <!-- 轨道底图 -->
          <!-- 下行轨道 -->
          <line x1="70" y1="48" x2="990" y2="48" stroke="#334155" stroke-width="4" stroke-linecap="round" />
          <text x="35" y="32" fill="#38bdf8" font-size="11" font-weight="bold">1号台 (下行)</text>
          
          <!-- 上行轨道 -->
          <line x1="70" y1="122" x2="990" y2="122" stroke="#334155" stroke-width="4" stroke-linecap="round" />
          <text x="35" y="146" fill="#34d399" font-size="11" font-weight="bold">2号台 (上行)</text>
          
          <!-- 西端折返环线 -->
          <path id="path-start-terminal" d="M 70 122 C 20 122, 20 48, 70 48" fill="none" stroke="#38bdf8" stroke-width="3" stroke-dasharray="4 2" />
          <text id="label-start-terminal" x="12" y="85" fill="#38bdf8" font-size="9" font-weight="bold" text-anchor="middle" transform="rotate(-90 12 85)">西端折返线</text>

          <!-- 动态渡线容器 -->
          <g id="svg-crossovers"></g>

          <!-- 东端大交路折返环线 -->
          <path id="path-end-terminal" d="M 990 48 C 1040 48, 1040 122, 990 122" fill="none" stroke="#38bdf8" stroke-width="3" stroke-dasharray="4 2" />
          <text id="label-end-terminal" x="1048" y="85" fill="#38bdf8" font-size="9" font-weight="bold" text-anchor="middle" transform="rotate(90 1048 85)">东端折返线</text>

          <!-- 车站节点容器 -->
          <g id="svg-stations"></g>
          <!-- 动态运行列车容器 -->
          <g id="svg-trains"></g>
        </svg>
      </div>
    </div>
  </div>

  <div class="grid">
    <!-- 1. 车站精准列车调度 -->
    <div class="panel" style="border-color: #38bdf8;">
      <h2>🚉 {line_name} · 站台列车运行调度</h2>
      
      <div class="form-group">
        <label>选择目标车站</label>
        <select id="station_select" onchange="onStationOrPlatformChange()">
          {station_options}
        </select>
      </div>

      <div class="form-group">
        <label>选择站台方向</label>
        <select id="platform_select" onchange="onStationOrPlatformChange()">
          <option value="1" selected>1号站台 (下行)</option>
          <option value="2">2号站台 (上行)</option>
        </select>
      </div>

      <div class="status-box" id="current-st-info" style="font-size: 12px;">
        <div>当前状态：<span id="st-status-badge" class="badge">加载中</span></div>
        <div>本趟预告：开往 <strong id="st-dest-text">--</strong> · 倒计时 <strong id="st-cd-text">-</strong> 分钟</div>
      </div>

      <div class="form-group">
        <label>本趟运行状态 (演示模式下由波浪引擎自动推进)</label>
        <select id="trip1_status">
          <option value="COUNTDOWN">正常区间倒计时 (COUNTDOWN)</option>
          <option value="ARRIVING">🟡 即将到站 (Will Be Arriving)</option>
          <option value="ARRIVED">🟢 列车到站 (Train Arrived)</option>
          <option value="NONSTOP">🔴 不停靠 (Non-Stop)</option>
          <option value="OUT_OF_SERVICE">⛔ 退出服务 (Out of service)</option>
        </select>
      </div>

      <div class="form-group">
        <label>本趟倒计时 (分钟)</label>
        <input type="number" id="trip1_countdown" value="3" min="0" max="99">
      </div>

      <div class="form-group">
        <label>本趟终点站</label>
        <select id="trip1_dest">
          {station_options}
        </select>
      </div>

      <div class="btn-grid">
        <button onclick="updateTrip()">🚀 设为本趟状态</button>
        <button class="success" onclick="restoreAutoTrip()">🟢 恢复自动仿真</button>
      </div>
    </div>

    <!-- 2. 全线视频母钟播控 -->
    <div class="panel">
      <h2>🎬 全线视频母钟中央播控</h2>
      
      <div class="status-box" id="video-monitor-box">
        <div><strong>当前播出：</strong><span id="v-name" style="color: #38bdf8; font-weight: bold;">加载中...</span></div>
        <div><strong>母钟进度：</strong><span id="v-time">00:00 / 00:00</span>（剩余 <span id="v-remain">0</span>s）</div>
        <div class="progress-bar-bg"><div class="progress-bar-fill" id="v-progress"></div></div>
        <div style="margin-top: 6px;">
          <span class="badge" id="v-loop-badge">自动轮播中</span>
          <span class="badge" id="v-black-badge">正常播放</span>
        </div>
      </div>

      <div class="form-group">
        <label>快速切播指定视频 (动态识别 Video*.mp4)</label>
        <div id="video-btn-list" class="btn-grid"></div>
      </div>

      <div class="form-group" style="margin-top: 10px;">
        <label>🎯 精准进度跳转与拖拽 (秒)</label>
        <div style="display: flex; gap: 8px; align-items: center;">
          <input type="range" id="seek-range" min="0" max="100" step="1" style="flex: 1; cursor: pointer;" onchange="seekVideo(this.value)">
          <input type="number" id="seek-input" min="0" max="999" placeholder="秒" style="width: 65px;">
          <button style="width: auto; margin-top: 0; padding: 7px 12px;" onclick="seekVideo(document.getElementById('seek-input').value)">跳转</button>
        </div>
      </div>

      <div class="btn-grid" style="margin-top: 6px;">
        <button class="secondary" onclick="stepVideo(-10)">⏪ 快退 10 秒</button>
        <button class="secondary" onclick="stepVideo(10)">⏩ 快进 10 秒</button>
      </div>

      <div class="btn-grid" style="margin-top: 6px;">
        <button class="secondary" onclick="nextVideo()">⏭️ 立即切下一部</button>
        <button class="danger" id="btn-black" onclick="toggleBlackScreen()">⏹️ 全线黑屏待机</button>
      </div>
      <div class="btn-grid" style="margin-top: 6px;">
        <button class="secondary" id="btn-loop" onclick="toggleAutoLoop()">🔄 切换自动轮播</button>
        <button class="secondary" onclick="rescanVideos()">📁 重新扫描 Videos/</button>
      </div>
    </div>

    <!-- 3. 突发应急与全线广播 -->
    <div class="panel" style="border-color: #f43f5e;">
      <h2 style="color: #f43f5e;">⚠️ 突发应急与运营跑马灯</h2>
      <div class="form-group">
        <label>应急目标车站</label>
        <select id="emergency_station">
          {station_options}
        </select>
      </div>
      <div class="form-group">
        <label>应急广播词</label>
        <textarea id="emergency_msg" rows="2">车站发生紧急情况，请听从工作人员指挥有序疏散！</textarea>
      </div>
      <button class="danger" onclick="triggerEmergency(true)">🚨 触发车站紧急疏散模式</button>
      <button class="success" onclick="triggerEmergency(false)">✅ 解除应急状态，恢复常态</button>

      <button class="danger" style="margin-top: 10px; background: #991b1b;" onclick="setLineOutOfService()">⛔ 设为全线退出服务 (Out of service)</button>
      <button class="secondary" style="margin-top: 6px;" onclick="restoreAllAuto()">🟢 恢复全线自动行车时刻表</button>

      <div class="form-group" style="margin-top: 20px;">
        <label>全线底部跑马灯 (Ticker)</label>
        <input type="text" id="ticker_text" value="欢迎乘坐城市轨道交通！请先下后上，注意站台间隙。">
      </div>
      <button onclick="setTicker()">📝 更新全线滚动公告</button>
    </div>
  </div>

  <script>
    async function apiPost(data) {{
      const res = await fetch('/api/dispatch', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(data)
      }});
      return await res.json();
    }}

    let isBlackState = false;
    let isAutoLoopState = true;
    let currentSignalingMode = "DEMO";
    let cachedStationsData = {{}};

    async function pollVideoStatus() {{
      try {{
        const res = await fetch('/api/video_status');
        const v = await res.json();
        
        isBlackState = v.is_black;
        isAutoLoopState = v.auto_loop;
        currentSignalingMode = v.signaling_mode;
        cachedStationsData = v.stations || {{}};

        // 1. 更新在线屏幕数与清单
        document.getElementById('online-count-badge').textContent = `在线屏幕: ${{v.online_count}}`;
        const screensTagList = document.getElementById('screens-tag-list');
        screensTagList.innerHTML = '';
        if (v.online_screens && v.online_screens.length > 0) {{
          document.getElementById('screens-summary').textContent = `共 ${{v.online_screens.length}} 台设备已接入`;
          v.online_screens.forEach(s => {{
            const tag = document.createElement('span');
            tag.className = 'screen-tag';
            tag.textContent = `🟢 ${{s.station_cn}} ${{s.platform}}台 (${{s.device_id}})`;
            screensTagList.appendChild(tag);
          }});
        }} else {{
          document.getElementById('screens-summary').textContent = '暂无屏幕接入';
        }}

        // 2. 更新模式 Badge
        const modeBadge = document.getElementById('mode-badge');
        if (modeBadge) {{
          if (v.is_ats_online) {{
            modeBadge.textContent = '🟢 ATS 信号在线接入';
            modeBadge.className = 'badge badge-success';
          }} else {{
            modeBadge.textContent = '⚪ ATS 信号离线 / 手动待机';
            modeBadge.className = 'badge badge-warn';
          }}
        }}

        // 3. 更新视频母钟监视
        document.getElementById('v-name').textContent = v.is_black ? '⚫ 已黑屏待机' : (v.name || '无视频');
        const elMin = Math.floor(v.elapsed / 60);
        const elSec = Math.floor(v.elapsed % 60);
        const durMin = Math.floor(v.duration / 60);
        const durSec = Math.floor(v.duration % 60);
        const fmt = (m, s) => `${{m.toString().padStart(2, '0')}}:${{s.toString().padStart(2, '0')}}`;
        
        document.getElementById('v-time').textContent = `${{fmt(elMin, elSec)}} / ${{fmt(durMin, durSec)}}`;
        document.getElementById('v-remain').textContent = Math.round(v.remaining);

        const pct = v.duration > 0 ? Math.min(100, (v.elapsed / v.duration) * 100) : 0;
        document.getElementById('v-progress').style.width = pct + '%';

        const seekRange = document.getElementById('seek-range');
        if (seekRange && !seekRange.matches(':active')) {{
          seekRange.max = v.duration;
          seekRange.value = v.elapsed;
        }}

        const loopBadge = document.getElementById('v-loop-badge');
        loopBadge.textContent = v.auto_loop ? '自动轮播中' : '轮播已暂停';
        loopBadge.className = 'badge ' + (v.auto_loop ? 'badge-success' : 'badge-warn');

        const blackBadge = document.getElementById('v-black-badge');
        blackBadge.textContent = v.is_black ? '黑屏待机中' : '正常播放';
        blackBadge.className = 'badge ' + (v.is_black ? 'badge-danger' : 'badge-success');

        document.getElementById('btn-black').textContent = v.is_black ? '▶️ 恢复正常播放' : '⏹️ 全线黑屏待机';
        document.getElementById('btn-loop').textContent = v.auto_loop ? '⏸️ 暂停自动轮播' : '▶️ 开启自动轮播';

        // 4. 渲染视频列表按钮
        const listDiv = document.getElementById('video-btn-list');
        if (listDiv.childElementCount !== v.playlist.length) {{
          listDiv.innerHTML = '';
          v.playlist.forEach((p, idx) => {{
            const btn = document.createElement('button');
            btn.style.fontSize = '12px';
            btn.style.padding = '6px';
            btn.textContent = `▶️ ${{p.name}} (${{Math.round(p.duration)}}s)`;
            btn.onclick = () => switchVideo(idx);
            listDiv.appendChild(btn);
          }});
        }}

        // 5. 渲染运行图
        // 5. 渲染运行图
        if (v.active_trains) {{
          renderTrainGraph(v.active_trains);
        }}

        // 6. 更新所选车站的表单显示（演示模式下实时刷新）
        updateStationFormValues();

      }} catch (e) {{
        console.error(e);
      }}
    }}

    const LINE_CONFIG = {line_json_str};
    const STATIONS_DATA = LINE_CONFIG.stations || [];
    const NUM_STATIONS = STATIONS_DATA.length;
    const ROUTING = LINE_CONFIG.routing_pattern || {{}};
    const SHORT_TERM = ROUTING.short_turn_terminal !== undefined ? ROUTING.short_turn_terminal : 20;
    const FULL_TERM = ROUTING.full_turn_terminal !== undefined ? ROUTING.full_turn_terminal : (NUM_STATIONS - 1);
    const START_TERM = ROUTING.start_terminal !== undefined ? ROUTING.start_terminal : 0;

    function renderTrainGraph(trains) {{
        // 1. 初始化车站与轨道 SVG (只运行一次)
        if (!document.getElementById('svg-station-node-0')) {{
            const stGroup = document.getElementById('svg-stations');
            let html = '';
            
            // 动态设置渡线与终点折返位置
            const x_full = 70 + FULL_TERM * 36.8;
            
            // 动态渲染所有渡线 (has_crossover)
            let crossHtml = '';
            STATIONS_DATA.forEach(st => {{
                if (st.has_crossover) {{
                    const cx = 70 + st.id * 36.8;
                    crossHtml += `<path d="M ${{cx}} 48 C ${{cx + 22}} 48, ${{cx + 29}} 85, ${{cx + 12}} 108 L ${{cx}} 122" fill="none" stroke="#c084fc" stroke-width="3" stroke-dasharray="3 3" />`;
                    crossHtml += `<text x="${{cx + 26}}" y="88" fill="#c084fc" font-size="8.5" font-weight="bold">${{st.short || st.cn}}渡线</text>`;
                }}
            }});
            const crossContainer = document.getElementById('svg-crossovers');
            if (crossContainer) crossContainer.innerHTML = crossHtml;
            
            const pEnd = document.getElementById('path-end-terminal');
            if (pEnd) pEnd.setAttribute('d', `M ${{x_full}} 48 C ${{x_full + 50}} 48, ${{x_full + 50}} 122, ${{x_full}} 122`);
            const lEnd = document.getElementById('label-end-terminal');
            if (lEnd) {{
              lEnd.setAttribute('x', x_full + 58);
              lEnd.textContent = (STATIONS_DATA[FULL_TERM]?.short || '终点') + '折返线';
            }}

            const lStart = document.getElementById('label-start-terminal');
            if (lStart) lStart.textContent = (STATIONS_DATA[START_TERM]?.short || '始发') + '折返线';

            for (let i = 0; i < NUM_STATIONS; i++) {{
                const x = 70 + i * 36.8;
                html += `<line id="svg-station-node-${{i}}" x1="${{x}}" y1="48" x2="${{x}}" y2="122" stroke="#1e293b" stroke-width="2" />`;
                html += `<circle cx="${{x}}" cy="48" r="4" fill="#64748b" />`;
                html += `<circle cx="${{x}}" cy="122" r="4" fill="#64748b" />`;
                const idStr = i < 10 ? '0' + i : i;
                const name = STATIONS_DATA[i]?.short || STATIONS_DATA[i]?.cn || `S${{i}}`;
                html += `<text x="${{x}}" y="76" fill="#94a3b8" font-size="9" font-weight="bold" text-anchor="middle">${{idStr}}</text>`;
                html += `<text x="${{x}}" y="96" fill="#cbd5e1" font-size="8.5" text-anchor="middle">${{name}}</text>`;
            }}
            stGroup.innerHTML = html;
        }}

        // 2. 动态渲染所有实时列车（包含上下行与各折返段）
        const trGroup = document.getElementById('svg-trains');
        let trHtml = '';
        trains.forEach(t => {{
            let tx = 0, ty = 0;
            let label = '', fill = '#0284c7', stroke = '#38bdf8';
            let w = 30, h = 20;
            const termDest = STATIONS_DATA[t.dest]?.short?.substring(0, 1) || '终';
            const startDest = STATIONS_DATA[START_TERM]?.short?.substring(0, 1) || '始';

            if (t.dir === 1) {{
                // 下行运行
                tx = 70 + t.pos * 36.8;
                ty = 48;
                label = `▶ ${{termDest}}`;
                fill = '#0284c7'; stroke = '#38bdf8';
            }} else if (t.dir === 2) {{
                // 上行运行
                tx = 70 + t.pos * 36.8;
                ty = 122;
                label = `◀ ${{termDest}}`;
                fill = '#059669'; stroke = '#34d399';
            }} else if (t.dir === 3) {{
                // 始发站折返线 (上行 -> 下行)
                const p = t.progress || 0;
                tx = 70 - Math.sin(p * Math.PI) * 45;
                ty = 122 - p * 74;
                label = '🔄折返';
                fill = '#7e22ce'; stroke = '#c084fc';
                w = 34;
            }} else if (t.dir === 4) {{
                // 渡线折返 (下行/上行 -> 反向)
                const p = t.progress || 0;
                const dest_x = 70 + t.pos * 36.8;
                tx = dest_x + Math.sin(p * Math.PI) * 22;
                ty = 48 + p * 74;
                label = '🔄折返';
                fill = '#7e22ce'; stroke = '#c084fc';
                w = 34;
            }} else if (t.dir === 5) {{
                // 大交路终点折返线 (下行 -> 上行)
                const p = t.progress || 0;
                const x_full = 70 + FULL_TERM * 36.8;
                tx = x_full + Math.sin(p * Math.PI) * 45;
                ty = 48 + p * 74;
                label = '🔄折返';
                fill = '#7e22ce'; stroke = '#c084fc';
                w = 34;
            }}

            trHtml += `<g transform="translate(${{tx}}, ${{ty}})">
                <rect x="${{-w/2}}" y="${{-h/2}}" width="${{w}}" height="${{h}}" rx="4" fill="${{fill}}" stroke="${{stroke}}" stroke-width="1.5" />
                <text x="0" y="3.5" fill="#ffffff" font-size="9" font-weight="bold" text-anchor="middle">${{label}}</text>
            </g>`;
        }});
        trGroup.innerHTML = trHtml;
    }}

    function updateStationFormValues() {{
      const stId = parseInt(document.getElementById('station_select').value);
      const pfId = parseInt(document.getElementById('platform_select').value);
      const stData = cachedStationsData[stId] && cachedStationsData[stId][pfId];
      if (!stData) return;

      const t1 = stData.trip1;
      const t2 = stData.trip2;

      document.getElementById('st-status-badge').textContent = t1.status;
      document.getElementById('st-cd-text').textContent = t1.countdown;
      const destName = STATIONS_DATA[t1.dest]?.cn || ('站号 ' + t1.dest);
      const destEl = document.getElementById('st-dest-text');
      if (destEl) destEl.textContent = destName;

      // 如果不是在主动编辑输入框，自动对齐
      const activeEl = document.activeElement;
      if (activeEl.id !== 'trip1_countdown') document.getElementById('trip1_countdown').value = t1.countdown;
      if (activeEl.id !== 'trip1_status') document.getElementById('trip1_status').value = t1.status;
      if (activeEl.id !== 'trip1_dest') document.getElementById('trip1_dest').value = t1.dest;
      if (activeEl.id !== 'trip2_countdown') document.getElementById('trip2_countdown').value = t2.countdown;
    }}

    function onStationOrPlatformChange() {{
      updateStationFormValues();
    }}

    function onLineChange(lineId) {{
      window.location.href = '/control?line=' + lineId;
    }}

    setInterval(pollVideoStatus, 1000);
    pollVideoStatus();

    function seekVideo(sec) {{
      sec = parseFloat(sec) || 0;
      apiPost({{ action: 'SEEK_VIDEO', time: sec }});
    }}

    function stepVideo(delta) {{
      fetch('/api/video_status').then(r => r.json()).then(v => {{
        seekVideo(Math.max(0, Math.min(v.duration, v.elapsed + delta)));
      }});
    }}

    function switchVideo(idx) {{
      apiPost({{ action: 'SWITCH_VIDEO', index: idx }});
    }}

    function nextVideo() {{
      apiPost({{ action: 'NEXT_VIDEO' }});
    }}

    function toggleBlackScreen() {{
      apiPost({{ action: 'BLACK_SCREEN', active: !isBlackState }});
    }}

    function showToast(msg, type='success') {{
      const container = document.getElementById('toast-container');
      if (!container) return;
      const toast = document.createElement('div');
      toast.style.background = type === 'danger' ? '#e11d48' : (type === 'warn' ? '#d97706' : '#059669');
      toast.style.color = '#ffffff';
      toast.style.padding = '10px 18px';
      toast.style.borderRadius = '8px';
      toast.style.boxShadow = '0 4px 12px rgba(0,0,0,0.4)';
      toast.style.fontSize = '13px';
      toast.style.fontWeight = '600';
      toast.style.transition = 'all 0.3s ease';
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(-10px)';
      toast.textContent = msg;
      container.appendChild(toast);
      
      requestAnimationFrame(() => {{
        toast.style.opacity = '1';
        toast.style.transform = 'translateY(0)';
      }});

      setTimeout(() => {{
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(-10px)';
        setTimeout(() => toast.remove(), 300);
      }}, 2000);
    }}

    function rescanVideos() {{
      apiPost({{ action: 'RESCAN_VIDEOS' }}).then(() => {{
        document.getElementById('video-btn-list').innerHTML = '';
        pollVideoStatus();
        showToast('🎬 视频库已重新扫描完成！', 'success');
      }});
    }}

    function updateTrip() {{
      const stId = parseInt(document.getElementById('station_select').value);
      const pfId = parseInt(document.getElementById('platform_select').value);
      const status = document.getElementById('trip1_status').value;
      const cd = document.getElementById('trip1_countdown').value;
      const dest = document.getElementById('trip1_dest').value;
      apiPost({{
        action: 'UPDATE_TRIP',
        station: stId,
        platform: pfId,
        trip1_countdown: cd,
        trip1_status: status,
        trip1_dest: dest
      }}).then(() => {{
        showToast(`✅ [${{stId}}号站] ${{pfId}}号台 已设为本趟状态`);
      }});
    }}

    function restoreAutoTrip() {{
      const stId = parseInt(document.getElementById('station_select').value);
      const pfId = parseInt(document.getElementById('platform_select').value);
      apiPost({{
        action: 'RESTORE_AUTO',
        station: stId,
        platform: pfId
      }}).then(() => {{
        showToast(`🟢 已恢复 [${{stId}}号站] ${{pfId}}号台为自动行车时刻表`, 'success');
      }});
    }}

    function triggerEmergency(active) {{
      const stId = parseInt(document.getElementById('emergency_station').value);
      apiPost({{
        action: 'TRIGGER_EMERGENCY',
        station: stId,
        active: active,
        message: document.getElementById('emergency_msg').value
      }}).then(() => showToast(active ? `🚨 车站 [${{stId}}] 紧急广播模式已下发！` : '✅ 应急状态已解除！', active ? 'danger' : 'success'));
    }}

    function setTicker() {{
      apiPost({{
        action: 'SET_TICKER',
        text: document.getElementById('ticker_text').value
      }}).then(() => showToast('📢 跑马灯滚动公告已更新！', 'success'));
    }}

    function setLineOutOfService() {{
      apiPost({{ action: 'LINE_OUT_OF_SERVICE' }}).then(() => {{
        showToast('⛔ 全线所有车站已切换为“退出服务”状态！', 'danger');
      }});
    }}

    function restoreAllAuto() {{
      apiPost({{ action: 'RESTORE_ALL_AUTO' }}).then(() => {{
        showToast('🟢 已恢复全线各站台为自动行车时刻表！', 'success');
      }});
    }}
  </script>
</body>
</html>
"""

# ===== 8. 挂载本地静态文件目录（直接在 8080 端口提供 index.html 和视频） =====
app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("🚇 Metro PIDS 中心大后端已启动在 http://0.0.0.0:8080")
    print("🎛️ OCC 调度中心控制台: http://localhost:8080/control")
    print("📺 PIS 站台屏幕前端: http://localhost:8080/")
    uvicorn.run(app, host="0.0.0.0", port=8080)


