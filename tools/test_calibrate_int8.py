# -*- coding: utf-8 -*-
"""calibrate_int8 的自测 —— 不需要板子，也不需要 RKNN-Toolkit2。

重点验证：
  1. 拿到非 JPEG（比如 8088 挂了返回 HTML 错误页）必须报错，不能当图存下来
  2. 重复帧会被跳过（连拍拿到的几十张几乎一样，会让校准集虚胖）
  3. 打出来的包必须是 LF、占位符全替换、校准列表用相对路径
  4. 对比环节「没数据」不能判通过

用法：
    python tools/test_calibrate_int8.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import calibrate_int8 as ci  # noqa: E402

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:                                   # pragma: no cover
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

PASS = 0
FAIL = 0
FAILURES = []

FAKE_JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        FAILURES.append(label)
        print("  FAIL %s\n         实得 %r\n         期望 %r" % (label, got, want))


class Args(object):
    """把 dict 包成 argparse 的 Namespace，省得每个子命令都拼参数。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeCamera(BaseHTTPRequestHandler):
    """按脚本配置决定返回 JPEG 还是 HTML。"""

    mode = "jpeg"
    payload = FAKE_JPEG
    counter = [0]

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if FakeCamera.mode == "html":
            body = b"<html>camera not ready</html>"
            ctype = "text/html"
        elif FakeCamera.mode == "empty":
            body = b""
            ctype = "image/jpeg"
        else:
            FakeCamera.counter[0] += 1
            # 前两张不一样，之后一直重复第二张 —— 用来验证去重
            body = FAKE_JPEG if FakeCamera.counter[0] == 1 else FAKE_JPEG + b"x"
            ctype = "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_fake_camera():
    server = HTTPServer(("127.0.0.1", 0), FakeCamera)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server, server.server_address[1]


def test_fetch_rejects_non_jpeg(port):
    print("\n[1] fetch_raw_frame —— 非 JPEG 必须报错，不能当图存")
    FakeCamera.mode = "html"
    data, error = ci.fetch_raw_frame("127.0.0.1", port, timeout=5)
    check("HTML 错误页 -> data 是 None", data, None)
    check("错误信息点明「不是 JPEG」", "不是 JPEG" in error, True)

    FakeCamera.mode = "empty"
    data2, error2 = ci.fetch_raw_frame("127.0.0.1", port, timeout=5)
    check("空响应 -> data 是 None", data2, None)
    check("错误信息是空响应", error2, "空响应")

    FakeCamera.mode = "jpeg"
    data3, error3 = ci.fetch_raw_frame("127.0.0.1", port, timeout=5)
    check("正常 JPEG 能取到", data3 is not None, True)
    check("正常时无错误", error3, "")


def test_collect_dedup(port, tmpdir):
    print("\n[2] collect —— 重复帧要跳过，列表要用相对路径")
    FakeCamera.mode = "jpeg"
    FakeCamera.counter[0] = 0
    out_dir = os.path.join(tmpdir, "calib")
    args = Args(board="127.0.0.1", port=port, count=5, interval=0.01,
                timeout=5, calib=out_dir, keep_duplicates=False)
    code = ci.cmd_collect(args)
    saved = sorted(f for f in os.listdir(out_dir) if f.endswith(".jpg"))
    check("退出码 0", code, 0)
    # 假相机会一直返回同一张（除了第一张），所以最终只该存下 2 张
    check("重复帧被跳过，只存 2 张", len(saved), 2)

    list_path = os.path.join(out_dir, "calib.txt")
    check("生成了 calib.txt", os.path.isfile(list_path), True)
    with open(list_path) as fh:
        lines = [line.strip() for line in fh if line.strip()]
    check("列表行数 = 图片数", len(lines), len(saved))
    check("列表用相对路径（calib/ 前缀）",
          all(line.startswith("calib/") for line in lines), True)

    # 反向：允许重复时必须存满
    FakeCamera.counter[0] = 0
    out_dir2 = os.path.join(tmpdir, "calib_dup")
    args2 = Args(board="127.0.0.1", port=port, count=4, interval=0.01,
                 timeout=5, calib=out_dir2, keep_duplicates=True)
    ci.cmd_collect(args2)
    saved2 = [f for f in os.listdir(out_dir2) if f.endswith(".jpg")]
    check("keep-duplicates 时存满 4 张", len(saved2), 4)


def test_collect_fails_cleanly(port, tmpdir):
    print("\n[3] collect —— 取不到图要干净失败，不能留下空目录就返回 0")
    FakeCamera.mode = "html"
    out_dir = os.path.join(tmpdir, "calib_fail")
    args = Args(board="127.0.0.1", port=port, count=3, interval=0.01,
                timeout=3, calib=out_dir, keep_duplicates=False)
    code = ci.cmd_collect(args)
    check("退出码 1", code, 1)
    saved = [f for f in os.listdir(out_dir) if f.endswith(".jpg")] \
        if os.path.isdir(out_dir) else []
    check("一张图都没落盘", saved, [])
    FakeCamera.mode = "jpeg"


def test_pack(tmpdir):
    print("\n[4] pack —— LF、占位符、相对路径三件事都不能错")
    calib_dir = os.path.join(tmpdir, "packcalib")
    val_dir = os.path.join(tmpdir, "packval")
    os.makedirs(calib_dir)
    os.makedirs(val_dir)
    for index in range(1, 6):
        with open(os.path.join(calib_dir, "calib_%04d.jpg" % index), "wb") as fh:
            fh.write(FAKE_JPEG)
    for index in range(1, 4):
        with open(os.path.join(val_dir, "v_%04d.jpg" % index), "wb") as fh:
            fh.write(FAKE_JPEG)
    onnx = os.path.join(tmpdir, "m.onnx")
    with open(onnx, "wb") as fh:
        fh.write(b"ONNX")

    out = os.path.join(tmpdir, "bundle")
    args = Args(onnx=onnx, calib=calib_dir, out=out, target="rk3568",
                mean="0,0,0", std="255,255,255", val=val_dir, out_rknn="m_i8.rknn")
    code = ci.cmd_pack(args)
    check("退出码 0", code, 0)

    script = os.path.join(out, "vm_calibrate_int8.sh")
    with open(script, "rb") as fh:
        raw = fh.read()
    check("脚本存在", os.path.isfile(script), True)
    check("脚本是 LF（没有 \\r）", b"\r\n" not in raw, True)
    check("占位符全部替换", b"%(" not in raw, True)
    # Windows 的文件系统没有 POSIX 执行位，chmod 设了也读不回来。
    # 这不是 bug，但要显式记一笔 —— 所以调用方式一律写成 `bash vm_calibrate_int8.sh`。
    if os.name == "posix":
        check("脚本可执行位", bool(os.stat(script).st_mode & 0o111), True)
    else:
        check("Windows 上执行位不可设（预期行为）",
              bool(os.stat(script).st_mode & 0o111), False)

    with open(os.path.join(out, "calib.txt")) as fh:
        entries = [line.strip() for line in fh if line.strip()]
    check("校准列表条数", len(entries), 5)
    check("列表是相对路径", all(e.startswith("calib/") for e in entries), True)
    check("onnx 被拷进包", os.path.isfile(os.path.join(out, "m.onnx")), True)
    check("验证集被拷进包", os.path.isdir(os.path.join(out, "val")), True)

    with open(os.path.join(out, "meta.json")) as fh:
        meta = json.load(fh)
    check("meta 记录校准图数", meta["calib_images"], 5)
    check("meta 记录验证集数", meta["val_images"], 3)
    check("meta 的 val_dir 是相对路径", meta["val_dir"], "val")

    # 反向：onnx 不存在要拒绝
    bad = Args(onnx=os.path.join(tmpdir, "nope.onnx"), calib=calib_dir,
               out=os.path.join(tmpdir, "bundle2"), target="rk3568",
               mean="0,0,0", std="255,255,255", val=None, out_rknn="x.rknn")
    check("onnx 缺失 -> 退出码 1", ci.cmd_pack(bad), 1)

    # 反向：校准目录空要拒绝
    empty_dir = os.path.join(tmpdir, "emptycalib")
    os.makedirs(empty_dir)
    bad2 = Args(onnx=onnx, calib=empty_dir, out=os.path.join(tmpdir, "bundle3"),
                target="rk3568", mean="0,0,0", std="255,255,255", val=None,
                out_rknn="x.rknn")
    check("校准图缺失 -> 退出码 1", ci.cmd_pack(bad2), 1)


def test_compare(tmpdir):
    print("\n[5] compare —— 没数据不能判通过")
    good = os.path.join(tmpdir, "ok.json")
    with open(good, "w") as fh:
        json.dump({"val_images": 2,
                   "fp": [{"image": "a.jpg", "mean_abs": 1.0},
                          {"image": "b.jpg", "mean_abs": 2.0}],
                   "int8": [{"image": "a.jpg", "mean_abs": 1.01},
                            {"image": "b.jpg", "mean_abs": 2.02}]}, fh)
    check("偏移小 -> 退出码 0",
          ci.cmd_compare(Args(result=good, max_delta_pct=5.0)), 0)

    bad = os.path.join(tmpdir, "bad.json")
    with open(bad, "w") as fh:
        json.dump({"val_images": 2,
                   "fp": [{"image": "a.jpg", "mean_abs": 1.0}],
                   "int8": [{"image": "a.jpg", "mean_abs": 3.0}]}, fh)
    check("偏移大 -> 退出码 1",
          ci.cmd_compare(Args(result=bad, max_delta_pct=5.0)), 1)

    empty = os.path.join(tmpdir, "empty.json")
    with open(empty, "w") as fh:
        json.dump({"val_images": 2, "fp": [], "int8": []}, fh)
    check("无数据 -> 退出码 1（不是 0）",
          ci.cmd_compare(Args(result=empty, max_delta_pct=5.0)), 1)

    missing = os.path.join(tmpdir, "nothere.json")
    check("文件不存在 -> 退出码 1",
          ci.cmd_compare(Args(result=missing, max_delta_pct=5.0)), 1)

    # 图片名对不上也要拒绝，不能拿空集算出「平均 0%」然后报通过
    mismatch = os.path.join(tmpdir, "mismatch.json")
    with open(mismatch, "w") as fh:
        json.dump({"val_images": 2,
                   "fp": [{"image": "a.jpg", "mean_abs": 1.0}],
                   "int8": [{"image": "zzz.jpg", "mean_abs": 1.5}]}, fh)
    check("图片名对不上 -> 退出码 1",
          ci.cmd_compare(Args(result=mismatch, max_delta_pct=5.0)), 1)


def main():
    tmpdir = os.environ.get("CALIB_TEST_DIR") or tempfile.mkdtemp(prefix="calib-test-")
    if not os.path.isdir(tmpdir):
        os.makedirs(tmpdir)
    server, port = start_fake_camera()
    print("假相机起在 127.0.0.1:%d，临时目录 %s" % (port, tmpdir))
    try:
        test_fetch_rejects_non_jpeg(port)
        test_collect_dedup(port, tmpdir)
        test_collect_fails_cleanly(port, tmpdir)
        test_pack(tmpdir)
        test_compare(tmpdir)
    finally:
        server.shutdown()
        server.server_close()
        if not os.environ.get("CALIB_TEST_DIR"):
            shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAIL:
        print("✗ %d 通过 / %d 失败" % (PASS, FAIL))
        for label in FAILURES:
            print("    - %s" % label)
        return 1
    print("✅ %d 项全部通过 —— 校准工具本身可信" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
