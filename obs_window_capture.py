"""OBS 窗口捕获方案：三个目标窗口各自捕获，摆到舞台框位

为什么不用显示器采集：本机（Y7000P 混合显卡 + HAGS）显示器采集一直黑屏，
窗口捕获走 WGC，跨显卡也没问题。

窗口按 exe 名模糊匹配（priority=2），标题变了也不怕。
"""
from __future__ import annotations

import time

import obsws_python as obs

SCENE = "大肥鱼直播间"

# (源名, 窗口标识 title:class:exe, [舞台 x,y,w,h])
TARGETS = [
    ("代码窗口",  "ZCode:Chrome_WidgetWin_1:ZCode.exe", [18, 96, 850, 968]),
    ("网站窗口",  "唯一的你:Chrome_WidgetWin_1:msedge.exe", [906, 96, 996, 424]),
    ("微信窗口",  "微信:Qt51514QWindowIcon:Weixin.exe", [906, 540, 996, 524]),
]


def main() -> None:
    cl = obs.ReqClient(host="127.0.0.1", port=4455, password="", timeout=10)

    items = {i["sourceName"]: int(i["sceneItemId"])
             for i in cl.get_scene_item_list(SCENE).scene_items}

    # 1) 关掉黑屏的显示器采集（不删，留作以后排查）
    if "真实桌面" in items:
        cl.set_scene_item_enabled(SCENE, items["真实桌面"], False)
        print("[·] 已隐藏显示器采集（黑屏，留作备用）")
    if "_tmp_probe" in items:
        cl.set_scene_item_enabled(SCENE, items["_tmp_probe"], False)

    # 2) 三个窗口捕获
    for name, win_id, (x, y, w, h) in TARGETS:
        settings = {
            "window": win_id,
            "priority": 2,          # 按 exe 名匹配
            "capture_mode": "auto",
            "cursor": True,
        }
        if name in items:
            cl.set_input_settings(name, settings, overlay=True)
            item_id = items[name]
            print(f"[·] 更新源 {name}")
        else:
            try:
                cl.create_input(SCENE, name, "window_capture", settings, True)
            except Exception:
                pass
            item_id = cl.get_scene_item_id(SCENE, name).scene_item_id
            print(f"[√] 添加源 {name}")
        cl.set_scene_item_enabled(SCENE, item_id, True)
        cl.set_scene_item_transform(SCENE, item_id, {
            "position": {"x": float(x), "y": float(y)},
            "scale": {"x": 1.0, "y": 1.0},
            "bounds": {"x": float(w), "y": float(h)},
            "boundsType": "OBS_BOUNDS_SCALE_INNER",
            "boundsAlignment": 0,
            "rotation": 0.0,
        })

    # 3) 层级：三个窗口在最底，舞台最顶
    cl.set_scene_item_index(SCENE, items.get("大肥鱼舞台") or
                            cl.get_scene_item_id(SCENE, "大肥鱼舞台").scene_item_id, 3)
    print("[√] 层级 OK（窗口捕获在下，舞台在上）")
    time.sleep(2)


if __name__ == "__main__":
    main()
