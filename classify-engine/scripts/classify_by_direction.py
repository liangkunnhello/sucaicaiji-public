#!/usr/bin/env python3
"""纯方向分类器 - 只按内容方向分类，不区分产品"""
import argparse
import base64
import json
import os
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

# 25个标准方向（从规则文件提取）
DIRECTIONS = [
    "图标", "盾牌锁", "月亮", "插画", "家庭亲子插画", "左右图文", 
    "手机卡片", "科技蓝", "文字大字报", "妙思衍生", "老歌", "萌宠",
    "幸福生活", "一分购", "网赚", "美女网赚", "旅游攻略", "签到有礼",
    "小程序", "短剧", "上下左右图文", "盾牌", "大字报", "自动衍生", "诱导类"
]

def eprint(msg: str) -> None:
    print(f"[分类器] {msg}", file=sys.stderr, flush=True)

def emit(line: str) -> None:
    """向 stdout 输出 UI 协议行（与四证据引擎保持一致，供原界面解析）"""
    print(line, file=sys.stdout, flush=True)

def call_vision_api(image_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """调用视觉API判断方向"""
    import requests
    
    if not all(config.get(k) for k in ['api_endpoint', 'model_id', 'api_key']):
        raise RuntimeError("API配置不完整")
    
    # 编码图片
    media_type = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(
        image_path.suffix.lower(), "image/jpeg"
    )
    b64_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    
    # 构造prompt - 只判断方向
    prompt = f"""你是广告素材的视觉分析专家。请判断这张图片的**主要内容方向**。

可选方向列表：
{json.dumps(DIRECTIONS, ensure_ascii=False)}

判断规则：
1. 只根据**画面视觉内容**判断：构图、主体元素、配色、风格
2. 选择画面占比最大、最突出的特征对应的方向
3. 如果多个方向都很突出，或画面模糊不清，设置 abstain=true
4. confidence 表示画面证据的明确性（0-1），低于0.85应拒绝

只返回JSON（不要解释）：
{{"direction":"方向名称或待确认","confidence":0.0,"abstain":false,"reason":"画面中的视觉特征描述"}}
"""
    
    endpoint = config['api_endpoint'].rstrip("/") + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": config['api_key'],
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": config['model_id'],
        "max_tokens": 2000,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}},
                {"type": "text", "text": prompt}
            ]
        }]
    }
    
    response = requests.post(endpoint, headers=headers, json=payload, timeout=90)
    if response.status_code != 200:
        raise RuntimeError(f"API HTTP {response.status_code}: {response.text[:200]}")
    
    body = response.json()
    text = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
    
    # 清理JSON
    text = text.strip()
    if text.startswith("```json"): text = text[7:]
    elif text.startswith("```"): text = text[3:]
    if text.endswith("```"): text = text[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end+1]
    
    return json.loads(text.strip())

def classify_one(img: Path, config: dict[str, Any], min_confidence: float) -> dict[str, Any]:
    """分类单张图片，返回结果字典（不含文件操作）"""
    result = call_vision_api(img, config)
    direction = str(result.get("direction", "待确认")).strip()
    confidence = float(result.get("confidence", 0.0))
    abstain = bool(result.get("abstain", True))
    reason = str(result.get("reason", ""))

    if abstain or direction not in DIRECTIONS:
        return {"direction": "待确认", "status": "REVIEW", "kind": "abstain",
                "confidence": confidence, "reason": reason or f"abstain={abstain}"}
    if confidence < min_confidence:
        return {"direction": "待确认", "status": "REVIEW", "kind": "low_confidence",
                "confidence": confidence, "reason": f"置信度{confidence:.2f}<{min_confidence}"}
    return {"direction": direction, "status": "AUTO", "kind": "auto",
            "confidence": confidence, "reason": reason}


def _unique_target(target_dir: Path, name: str) -> Path:
    """目标目录下生成不重名的目标路径"""
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / name
    if target_file.exists():
        stem, suffix = target_file.stem, target_file.suffix
        counter = 1
        while target_file.exists():
            target_file = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1
    return target_file


def place_file(img: Path, direction: str, output_dir: Path,
               action: str = "copy", backup_dir: "Path | None" = None,
               source_root: "Path | None" = None) -> None:
    """把图片按指定动作放到目标方向目录。
    action:
      copy        仅复制（原图保留）
      move        移动（原图删除）
      backup-move 先把原图备份到 backup_dir，再移动
    重名自动追加序号。
    """
    target_file = _unique_target(output_dir / direction, img.name)

    # backup-move：移动前先把原图快照到备份目录（保留相对源目录的层级）
    if action == "backup-move" and backup_dir is not None:
        try:
            rel = img.relative_to(source_root) if source_root else Path(img.name)
        except ValueError:
            rel = Path(img.name)
        backup_target = backup_dir / rel
        backup_target.parent.mkdir(parents=True, exist_ok=True)
        if not backup_target.exists():
            shutil.copy2(img, backup_target)
        else:
            shutil.copy2(img, _unique_target(backup_target.parent, backup_target.name))

    if action in ("move", "backup-move"):
        shutil.move(str(img), str(target_file))
    else:
        shutil.copy2(img, target_file)


def classify_images(source_dir: Path, output_dir: Path, config_path: Path,
                   min_confidence: float = 0.85, dry_run: bool = False,
                   parallel: int = 5, action: str = "copy") -> None:
    """批量分类图片，输出 UI 协议行供原界面解析。
    action: copy / move / backup-move
    """
    config = json.loads(config_path.read_text(encoding='utf-8'))

    extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    images = [f for f in source_dir.rglob('*') if f.suffix.lower() in extensions and f.is_file()]
    total = len(images)
    action_label = {"copy": "复制", "move": "移动", "backup-move": "备份后移动"}.get(action, action)
    eprint(f"找到 {total} 张图片 · 文件动作：{action_label}")
    emit(f"FOUND|{total}")
    emit(f"STAGE|HUMAN|READY|纯方向模式 · 仅 API 判定 · {action_label}")

    if not images:
        eprint("没有找到图片文件")
        emit("SUMMARY|AUTO=0|REVIEW=0|TOTAL=0")
        emit(f"OUTPUT|{output_dir}")
        return

    # 备份目录：源目录同级，_备份_YYYYMMDD
    backup_dir = None
    if action == "backup-move" and not dry_run:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = source_dir.parent / f"{source_dir.name}_备份_{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        eprint(f"备份目录：{backup_dir}")
        emit(f"BACKUP|{backup_dir}")

    stats = {"auto": 0, "abstain": 0, "low_confidence": 0, "error": 0}
    review_folders = {
        "abstain": "主动拒绝",
        "low_confidence": "置信度不足",
    }
    direction_counts: dict[str, int] = {}
    lock = threading.Lock()
    counter = {"done": 0}

    def worker(img: Path) -> None:
        try:
            res = classify_one(img, config, min_confidence)
            direction = res["direction"]
            status = res["status"]
            if not dry_run:
                if status == "AUTO":
                    target_root = output_dir / "自动分类"
                    target_folder = direction
                else:
                    target_root = output_dir / "待确认"
                    target_folder = review_folders.get(res["kind"], "其他")
                place_file(img, target_folder, target_root, action=action,
                           backup_dir=backup_dir, source_root=source_dir)
            with lock:
                stats["auto" if res["kind"] == "auto" else res["kind"]] = \
                    stats.get(res["kind"], 0) + 1
                direction_counts[direction] = direction_counts.get(direction, 0) + 1
                counter["done"] += 1
                done = counter["done"]
            eprint(f"[{done}/{total}] {img.name} → {direction} ({res['confidence']:.2f})")
            emit(f"API_PROGRESS|{done}|{total}")
            emit(f"PROGRESS|{done}|{total}|{status}||{direction}|{img.name}")
        except Exception as e:  # noqa: BLE001
            with lock:
                stats["error"] += 1
                direction_counts["待确认"] = direction_counts.get("待确认", 0) + 1
                counter["done"] += 1
                done = counter["done"]
            eprint(f"[{done}/{total}] {img.name} ✗ {e}")
            if not dry_run:
                try:
                    place_file(img, "处理错误", output_dir / "待确认", action=action,
                               backup_dir=backup_dir, source_root=source_dir)
                except Exception:  # noqa: BLE001
                    pass
            emit(f"PROGRESS|{done}|{total}|REVIEW||待确认|{img.name}")

    workers = max(1, min(10, parallel))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, images))

    review = stats["abstain"] + stats["low_confidence"] + stats["error"]
    emit(f"SUMMARY|AUTO={stats['auto']}|REVIEW={review}|TOTAL={total}")
    emit(f"OUTPUT|{output_dir}")

    eprint("\n=== 分类完成 ===")
    auto_pct = stats['auto'] * 100 // total if total else 0
    eprint(f"自动分类: {stats['auto']} ({auto_pct}%)")
    eprint(f"主动拒绝: {stats['abstain']}  置信度低: {stats['low_confidence']}  失败: {stats['error']}")
    eprint("\n=== 方向分布 ===")
    for direction, count in sorted(direction_counts.items(), key=lambda x: -x[1]):
        if count > 0:
            eprint(f"  {direction}: {count}")
    if not dry_run:
        eprint(f"\n输出目录: {output_dir}")

def main():
    parser = argparse.ArgumentParser(description="纯方向分类器 - 只按内容方向分类")
    parser.add_argument("--source", required=True, help="图片源目录")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument("--config", help="API配置文件路径", 
                       default=r"C:\Users\admin\Videos\portable\resources\app\userdata\classify_config.json")
    parser.add_argument("--min-confidence", type=float, default=0.85, help="最低置信度阈值")
    parser.add_argument("--parallel", type=int, default=5, help="并发路数（1-10）")
    parser.add_argument("--action", choices=["copy", "move", "backup-move"], default="copy",
                       help="文件动作：copy=复制 / move=移动 / backup-move=备份后移动")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不改动文件")
    
    args = parser.parse_args()
    
    source = Path(args.source)
    output = Path(args.output)
    config = Path(args.config)
    
    if not source.exists():
        print(f"错误: 源目录不存在: {source}", file=sys.stderr)
        return 1
    
    if not config.exists():
        print(f"错误: 配置文件不存在: {config}", file=sys.stderr)
        return 1
    
    try:
        classify_images(source, output, config, args.min_confidence, args.dry_run,
                       args.parallel, args.action)
        return 0
    except KeyboardInterrupt:
        eprint("\n用户中断")
        return 130
    except Exception as e:
        eprint(f"\n致命错误: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
