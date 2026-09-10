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
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
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
            "ticker": "欢迎乘坐示例线路1号线！请先下后上，注意站台间隙。",
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
                "ticker": line_config.get("ticker", f"欢迎乘坐{line_config.get('name_cn', '城市轨道交通')}！请先下后上，注意站台间隙。"),
            },
            2: {
                "trip1": {"dest": start_term, "countdown": 4, "status": "COUNTDOWN"},
                "trip2": {"dest": start_term, "countdown": 8, "status": "NORMAL"},
                "ticker": line_config.get("ticker", f"欢迎乘坐{line_config.get('name_cn', '城市轨道交通')}！请先下后上，注意站台间隙。"),
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
    "global_ticker": ACTIVE_LINE.get("ticker", f"欢迎乘坐{ACTIVE_LINE.get('name_cn', '城市轨道交通')}！请先下后上，注意站台间隙。"),
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
        print(f"屏幕已连接: [{screen_info.get('device_id')}] - 当前在线屏幕数: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            info = self.active_connections.pop(websocket)
            print(f"屏幕断开连接: [{info.get('device_id')}] - 剩余在线屏幕数: {len(self.active_connections)}")

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
                "countdown": station_data["trip1"].get("countdown", 3),
                "status": t1_status
            },
            "trip2": {
                "dest_id": t2_dest,
                "dest_cn": t2_meta["cn"],
                "dest_en": t2_meta["en"],
                "countdown": station_data["trip2"].get("countdown", 6),
                "status": station_data["trip2"].get("status", "NORMAL")
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
        "active_trains": LINE_TRAINS.get(int(dispatch_state.get("active_line_id") or 1), []),
        "stations": dispatch_state["stations"]
    }

# ===== 8. 内置 OCC 调度可视化控制台页面 (/control) 与初始化接口 =====
@app.get("/api/control_init")
async def control_init(line: Optional[int] = Query(None)):
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
            dispatch_state["global_ticker"] = ACTIVE_LINE.get("ticker", f"欢迎乘坐{ACTIVE_LINE.get('name_cn', '城市轨道交通')}！请先下后上，注意站台间隙。")
            print(f"🎛️ [OCC切换线路] 调度中心已切换至: {ACTIVE_LINE.get('name_cn', f'Line {line}')}")
    else:
        selected_line = LINES_REGISTRY.get(int(dispatch_state.get("active_line_id") or 1), ACTIVE_LINE)

    return {
        "line_config": selected_line,
        "active_line_id": selected_line["line_id"],
        "lines": [{"line_id": lid, "name_cn": lcfg.get("name_cn", f"Line {lid}")} for lid, lcfg in LINES_REGISTRY.items()]
    }

@app.get("/control")
async def control_panel():
    return FileResponse("control.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

# ===== 8. 挂载本地静态文件目录（直接在 8080 端口提供 index.html 和视频） =====
app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("🚇 Metro PIDS 中心大后端已启动在 http://0.0.0.0:8080")
    print("🎛️ OCC 调度中心控制台: http://localhost:8080/control")
    print("📺 PIS 站台屏幕前端: http://localhost:8080/")
    uvicorn.run(app, host="0.0.0.0", port=8080)


