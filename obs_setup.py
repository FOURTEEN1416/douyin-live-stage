"""OBS 自动配置：建场景「大肥鱼直播间」= 显示器采集 + 舞台叠加层

前置：OBS 已安装并启动过一次（首次运行向导关掉即可）。
用法：
    python obs_setup.py            连接本机 OBS (ws://127.0.0.1:4455) 并配置
    python obs_setup.py --password xxx
    python obs_setup.py --launch   顺便启动 OBS

做的事：
  1. 画布 1920×1080 / 30fps
  2. 新建场景「大肥鱼直播间」
  3. 显示器采集：裁掉上下各 80 物理像素（2560×1600 → 16:9），铺满画布
  4. 浏览器源 stage.html (1920×1080) 叠加在上面
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

STAGE_URL = "http://127.0.0.1:8790/stage.html"
SCENE = "大肥鱼直播间"

# 2560×1600 裁成 16:9：上下各裁 (1600-1440)/2 = 80 物理像素
CROP_TOP = 80
CROP_BOTTOM = 80


def connect(host: str, port: int, password: str, tries: int = 30):
    import obsws_python as obs

    last = None
    for i in range(tries):
        try:
            cl = obs.ReqClient(host=host, port=port, password=password, timeout=5)
            return cl
        except Exception as e:  # OBS 还没起来
            last = e
            print(f"  等待 OBS websocket… ({i + 1}/{tries}) {type(e).__name__}")
            time.sleep(2)
    raise SystemExit(f"连不上 OBS websocket：{last}")


def ensure_scene(cl, name: str) -> None:
    scenes = [s["sceneName"] for s in cl.get_scene_list().scenes]
    if name not in scenes:
        cl.create_scene(name)
        print(f"[√] 已创建场景 {name}")
    else:
        print(f"[·] 场景已存在 {name}")
    cl.set_current_program_scene(name)


def add_source(cl, scene: str, kind: str, name: str, settings: dict) -> int:
    """往场景里加输入源（已存在就复用），返回 sceneItemId"""
    ids = [i["sourceName"] for i in cl.get_scene_item_list(scene).scene_items]
    if name in ids:
        item_id = cl.get_scene_item_id(scene, name).scene_item_id
        print(f"[·] 源已存在 {name}")
        return item_id
    try:
        cl.create_input(scene, name, kind, settings, True)
    except Exception:
        pass  # 全局已存在，直接加进场景
    item_id = cl.get_scene_item_id(scene, name).scene_item_id
    print(f"[√] 已添加源 {name}")
    return item_id


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4455)
    ap.add_argument("--password", default="")
    ap.add_argument("--launch", action="store_true", help="没开 OBS 就先启动它")
    args = ap.parse_args()

    if args.launch:
        subprocess.Popen(["obs64", "--startvirtualcam", "--disable-updater-check"],
                         cwd=r"C:\Program Files\obs-studio\bin\64bit",
                         shell=False)
        time.sleep(6)

    cl = connect(args.host, args.port, args.password)

    # 1. 画布与帧率
    cl.set_video_settings(fps_numerator := 30, 1, 1920, 1080, 1920, 1080)
    print("[√] 画布 1920×1080 @30fps")

    # 2. 场景
    ensure_scene(cl, SCENE)

    # 3. 显示器采集（裁上下 → 16:9 → 铺满）
    item = add_source(cl, SCENE, "monitor_capture", "真实桌面", {})
    # obsws-python 没有 crop 方法，直接走原始请求
    cl.base_client.req("SetSceneItemCrop", {
        "sceneName": SCENE, "sceneItemId": item,
        "sceneItemCrop": {"left": 0, "right": 0,
                          "top": CROP_TOP, "bottom": CROP_BOTTOM}})
    cl.set_scene_item_transform(SCENE, item, {
        "position": {"x": 0, "y": 0},
        "scale": {"x": 1.0, "y": 1.0},
        "bounds": {"x": 1920, "y": 1080},
        "boundsType": "OBS_BOUNDS_SCALE_INNER",
        "boundsAlignment": 0,   # 居中
    })
    print(f"[√] 桌面已裁上下各 {CROP_TOP}px 并铺满画布")

    # 4. 舞台叠加层
    add_source(cl, SCENE, "browser_source", "大肥鱼舞台", {
        "url": STAGE_URL,
        "width": 1920,
        "height": 1080,
        "css": "",
        "reroute_audio": True,
        "restart_when_active": False,
    })
    # 保证叠加层在最上面
    items = cl.get_scene_item_list(SCENE).scene_items
    top_idx = max(int(i["sceneItemIndex"]) for i in items)
    my_idx = [int(i["sceneItemIndex"]) for i in items
              if i["sourceName"] == "大肥鱼舞台"][0]
    if my_idx != top_idx:
        cl.set_scene_item_index(SCENE, [i["sceneItemId"] for i in items
                                        if i["sourceName"] == "大肥鱼舞台"][0], top_idx)
    print("[√] 舞台叠加层已置顶")

    print("\n完成。OBS 里应该能看到：真实桌面 + 大肥鱼舞台。")
    print("下一步：抖音开播拿推流地址 → OBS 设置 → 推流 → 开始推流。")


if __name__ == "__main__":
    main()
