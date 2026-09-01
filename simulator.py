#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
🚇 Metro ATS Simulation Engine (独立信号仿真与波浪运行图发生器)
================================================================================
职责：
1. 动态加载 lines/*.json 线路拓扑与交路规则；
2. 闭环仿真列车行车物理坐标、进出站区间、大小交路套跑与渡线折返；
3. 计算全线站台到站预测（倒计时、状态、车厢拥挤度）；
4. 通过标准 HTTP REST API (POST /api/ats/update) 将信号遥测数据推送至 PIDS 中心服务器。
================================================================================
"""

import glob
import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Dict, List

# ===== 1. 动态加载线路配置库 =====
def load_all_lines() -> Dict[int, dict]:
    lines = {}
    lines_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lines")
    if not os.path.exists(lines_dir):
        os.makedirs(lines_dir, exist_ok=True)
    
    json_files = glob.glob(os.path.join(lines_dir, "*.json"))
    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
                line_id = int(data.get("line_id", 3))
                lines[line_id] = data
                print(f"🚇 [ATS仿真引擎] 加载线路配置: {data.get('name_cn', f'Line {line_id}')} (共 {len(data.get('stations', []))} 站)")
        except Exception as e:
            print(f"❌ 加载线路配置文件失败 {jf}: {e}")
    return lines
            
def load_timetables() -> dict:
    tt_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timetables.json")
    if os.path.exists(tt_file):
        try:
            with open(tt_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                print(f"⏱️ [ATS仿真引擎] 加载运行图时刻表配置: {list(data.keys())}")
                return data
        except Exception as e:
            print(f"❌ 读取 timetables.json 失败: {e}")
    return {}


# ===== 2. 运行图仿真模型 =====
class LineSimulationSignaling:
    """
    单条线路波浪式连续闭塞运行图仿真
    支持上下行多交路（由 timetables.json 配置驱动）
    """
    def __init__(self, line_config: dict, timetable_config: dict = None):
        self.config = line_config
        self.line_id = line_config.get("line_id", 1)
        tt = timetable_config or {}
        
        self.headway = tt.get("headway_sec", 180)
        self.interval = tt.get("station_interval_sec", 35)
        self.turnaround_dur = tt.get("turnaround_dur_sec", 35)
        self.stop_time = tt.get("stop_time_sec", 20)
        
        self.stations = line_config.get("stations", [])
        self.routing = tt.get("routing_pattern", {})
        self.start_terminal = self.routing.get("start_terminal", 0)
        self.full_terminal = self.routing.get("full_turn_terminal", max(0, len(self.stations) - 1))
        self.short_terminal = self.routing.get("short_turn_terminal", self.full_terminal)
        self.down_seq = self.routing.get("down_sequence") or self.routing.get("sequence", [self.full_terminal])
        self.up_seq = self.routing.get("up_sequence", [self.start_terminal])
        self.start_epoch = time.time()

    def get_all_trains(self) -> list:
        """计算当前时刻全线在轨运行的所有列车物理坐标"""
        now = time.time()
        elapsed = now - self.start_epoch
        trains = []

        # 1. 下行车流 (1号台 · 往南/往东方向)
        k_down_min = int((elapsed - (self.full_terminal + 1) * self.interval) / self.headway)
        k_down_max = int(elapsed / self.headway) + 1

        for k in range(k_down_min, k_down_max + 1):
            t_down = elapsed - k * self.headway
            pos = t_down / self.interval
            dest = self.down_seq[k % len(self.down_seq)]
            is_short = (dest != self.full_terminal)
            
            # 下行在线区间
            if 0 <= pos < dest:
                trains.append({
                    "id": f"D{k}", "dir": 1, "pos": round(pos, 3),
                    "dest": dest, "status": "RUNNING", "progress": 0
                })
            # 南端终点折返区间
            elif dest <= pos <= dest + (self.turnaround_dur / self.interval):
                p = (t_down - dest * self.interval) / self.turnaround_dur
                dir_type = 4 if is_short else 5
                trains.append({
                    "id": f"T{k}", "dir": dir_type, "pos": dest,
                    "dest": self.start_terminal, "status": "TURNING", "progress": min(1.0, max(0.0, round(p, 3)))
                })

        # 2. 上行车流 (2号台 · 往北/往西方向)
        m_min = int(elapsed / self.headway)
        m_max = int((elapsed + (self.full_terminal + 1) * self.interval) / self.headway) + 2

        for m in range(m_min - 2, m_max + 1):
            orig = self.down_seq[m % len(self.down_seq)]
            up_dest = self.up_seq[m % len(self.up_seq)]
            t_dep_orig = m * self.headway - orig * self.interval
            t_since_dep = elapsed - t_dep_orig
            pos = orig - (t_since_dep / self.interval)

            # 上行在线区间
            if up_dest <= pos < orig:
                trains.append({
                    "id": f"U{m}", "dir": 2, "pos": round(pos, 3),
                    "dest": up_dest, "status": "RUNNING", "progress": 0
                })
            # 北端终点折返区间
            elif up_dest - (self.turnaround_dur / self.interval) <= pos < up_dest:
                p = (elapsed - (m * self.headway - (orig - up_dest) * self.interval)) / self.turnaround_dur
                if 0 <= p <= 1.0:
                    dir_type = 4 if (up_dest != self.start_terminal) else 3
                    trains.append({
                        "id": f"T_N_{m}", "dir": dir_type, "pos": up_dest,
                        "dest": self.full_terminal, "status": "TURNING", "progress": round(p, 3)
                    })

        return trains

    def get_station_platform_state(self, station_id: int, platform_id: int) -> dict:
        """预测特定车站、特定站台的本趟/下一趟到站车次及倒计时"""
        now = time.time()
        elapsed = now - self.start_epoch
        valid_arrivals = []
        
        if platform_id == 1:
            # 下行预告
            k_curr = int((elapsed - station_id * self.interval) / self.headway)
            for k in range(k_curr - 1, k_curr + 6):
                dest = self.down_seq[k % len(self.down_seq)]
                if station_id > dest:
                    continue
                t_arr = k * self.headway + station_id * self.interval
                time_to_reach = t_arr - elapsed
                if time_to_reach >= -self.stop_time:
                    valid_arrivals.append((time_to_reach, dest))
        else:
            # 上行预告
            m_curr = int((elapsed + station_id * self.interval) / self.headway)
            for m in range(m_curr - 1, m_curr + 6):
                orig = self.down_seq[m % len(self.down_seq)]
                up_dest = self.up_seq[m % len(self.up_seq)]
                if station_id > orig or station_id < up_dest:
                    continue
                t_arr = m * self.headway - station_id * self.interval
                time_to_reach = t_arr - elapsed
                if time_to_reach >= -self.stop_time:
                    valid_arrivals.append((time_to_reach, up_dest))

        valid_arrivals.sort(key=lambda x: x[0])
        
        trips = []
        for time_to_reach, dest in valid_arrivals[:2]:
            if dest == station_id and time_to_reach <= 0:
                # 折返清客车：到达该趟终点站后显示退出服务
                status = "OUT_OF_SERVICE"
                cd = 0
            elif time_to_reach <= 0:
                status = "ARRIVED"
                cd = 0
            elif time_to_reach <= 30:
                status = "ARRIVING"
                cd = 0
            else:
                status = "COUNTDOWN"
                cd = max(1, int(time_to_reach / 60) + 1)
            trips.append({"dest": dest, "countdown": cd, "status": status})
            
        while len(trips) < 2:
            trips.append({"dest": self.full_terminal if platform_id == 1 else self.start_terminal, "countdown": 0, "status": "OUT_OF_SERVICE"})
            
        return {
            "trip1": trips[0],
            "trip2": trips[1]
        }

    def get_full_line_state(self) -> dict:
        """获取全线所有车站所有站台的即时预告状态集合"""
        stations_dict = {}
        for s in self.stations:
            st_id = s["id"]
            stations_dict[st_id] = {
                1: self.get_station_platform_state(st_id, 1),
                2: self.get_station_platform_state(st_id, 2)
            }
        return stations_dict


# ===== 3. 主循环与 API 数据推送守护 =====
def run_ats_engine(server_url: str = "http://localhost:8080/api/ats/update", interval_sec: float = 1.0):
    print("================================================================================")
    print("🚀 [ATS 信号仿真引擎] 独立行车调度进程已启动")
    print(f"📡 目标 PIDS 中心服务器: {server_url}")
    print(f"⏱️ 仿真心跳频率: {interval_sec}s")
    print("================================================================================")
    
    lines = load_all_lines()
    timetables = load_timetables()
    engines = {lid: LineSimulationSignaling(cfg, timetables.get(str(lid), {})) for lid, cfg in lines.items()}
    
    tick = 0
    while True:
        tick += 1
        for line_id, engine in engines.items():
            trains = engine.get_all_trains()
            stations_state = engine.get_full_line_state()
            
            payload = {
                "line_id": line_id,
                "trains": trains,
                "stations": stations_state,
                "timestamp": time.time()
            }
            
            data_bytes = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                server_url,
                data=data_bytes,
                headers={'Content-Type': 'application/json'}
            )
            
            try:
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if tick % 10 == 0 and line_id == list(engines.keys())[0]:
                        line_name = lines[line_id].get("name_cn", f"Line {line_id}")
                        print(f"📡 [{time.strftime('%H:%M:%S')}] 线路 [{line_name}] 在线列车: {len(trains)} 列 | 遥测推送正常 (200 OK)")
            except urllib.error.URLError as e:
                if tick % 5 == 0:
                    print(f"⚠️ [{time.strftime('%H:%M:%S')}] 无法连接到 PIDS 中心服务器 ({server_url}): {e.reason}，将在下个周期重试...")
            except Exception as e:
                print(f"❌ 发送遥测异常: {e}")
                
        time.sleep(interval_sec)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080/api/ats/update"
    run_ats_engine(server_url=target)
