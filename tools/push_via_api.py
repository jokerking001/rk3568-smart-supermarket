# -*- coding: utf-8 -*-
"""在 github.com:443 被墙的网络下，改走 api.github.com 推送 git 提交。

背景：本网络只封 github.com:443（git smart-HTTP 端点），
但 api.github.com / codeload / raw.githubusercontent 都通；
SSH 密钥又没在 GitHub 注册。所以用 REST API 复刻提交。

★ 关键点：必须让远端生成的 commit SHA 与本地**完全一致**，
  否则本地 main 与远端 main 会分叉，下次正常 push 会 non-fast-forward。
  做法：blob 用同样内容 → tree 用同样 base + 同样 path/mode
        → commit 用同样 tree/parents/message/author/committer(含时间戳)。
"""
import base64
import json
import subprocess
import urllib.request

OWNER = "jokerking001"
REPO = "rk3568-smart-supermarket"
BRANCH = "main"
API = "https://api.github.com"


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, check=True, **kw).stdout


def get_token():
    p = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=20,
    )
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("拿不到 GitHub 凭据")


TOKEN = get_token()


def api(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "rk3568-push-helper")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}


def parse_commit(sha):
    """解析 git commit 对象为 API 需要的结构。"""
    raw = sh("git", "cat-file", "-p", sha)
    head, _, msg = raw.partition("\n\n")
    out = {"tree": None, "parents": [], "author": {}, "committer": {}}
    for line in head.splitlines():
        if line.startswith("tree "):
            out["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            out["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            out["author"] = _who(line[7:])
        elif line.startswith("committer "):
            out["committer"] = _who(line[10:])
    out["message"] = msg.rstrip("\n")
    return out


def _who(s):
    """'Name <mail> 1760000000 +0800' -> {'name','email','date'}"""
    lt = s.rindex("<")
    gt = s.index(">", lt)
    name = s[:lt].strip()
    email = s[lt + 1:gt]
    rest = s[gt + 1:].strip().split()
    date = "%s%s:%s" % (rest[0], rest[1][:3], rest[1][3:]) if len(rest) >= 2 else None
    # git 用 unix ts + tz；API 要 ISO8601，交给下面统一转换
    return {"name": name, "email": email, "_ts": rest[0] if rest else None,
            "_tz": rest[1] if len(rest) > 1 else "+0000"}


def iso(w):
    import datetime
    if not w.get("_ts"):
        return None
    off = w["_tz"]
    sign = 1 if off[0] == "+" else -1
    delta = datetime.timedelta(hours=int(off[1:3]), minutes=int(off[3:5])) * sign
    dt = datetime.datetime.fromtimestamp(int(w["_ts"]), datetime.timezone(delta))
    return dt.isoformat()


def main():
    head = sh("git", "rev-parse", "HEAD").strip()
    c = parse_commit(head)
    if len(c["parents"]) != 1:
        raise SystemExit("HEAD 不是单父提交，本脚本不适用")
    parent = c["parents"][0]
    # ★ base_tree 必须是**父提交**的 tree（HEAD 的 tree 远端还不存在，
    #   传过去会 422 "base_tree is not a valid tree oid"）。
    parent_tree = parse_commit(parent)["tree"]

    st, ref = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (OWNER, REPO, BRANCH))
    if st != 200:
        raise SystemExit("读远端 ref 失败: %s %s" % (st, ref))
    remote_sha = ref["object"]["sha"]
    print("本地 HEAD   :", head)
    print("本地 parent :", parent)
    print("远端 main   :", remote_sha)
    if remote_sha == head:
        print("✅ 远端已是最新，无需推送")
        return
    if remote_sha != parent:
        raise SystemExit(
            "❌ 远端 main (%s) 不是本地 HEAD 的父提交 (%s)。\n"
            "   说明远端有本地没有的提交，先别用本脚本，需要人工对齐。"
            % (remote_sha, parent))
    print("✅ 远端 == 本地父提交，可以安全复刻这一笔")

    changed = sh("git", "diff-tree", "--no-commit-id", "--name-only", "-r",
                 "%s..%s" % (parent, head)).split()
    print("本次改动文件:", changed)

    entries = []
    for path in changed:
        content = subprocess.run(["git", "show", "%s:%s" % (head, path)],
                                 capture_output=True).stdout
        st, r = api("POST", "/repos/%s/%s/git/blobs" % (OWNER, REPO),
                    {"content": base64.b64encode(content).decode(),
                     "encoding": "base64"})
        if st not in (200, 201):
            raise SystemExit("建 blob 失败 %s %s: %s" % (path, st, r))
        entries.append({"path": path, "mode": "100644", "type": "blob",
                        "sha": r["sha"]})
        print("  blob %-46s %s" % (path, r["sha"][:10]))

    st, tree = api("POST", "/repos/%s/%s/git/trees" % (OWNER, REPO),
                   {"base_tree": parent_tree, "tree": entries})
    if st not in (200, 201):
        raise SystemExit("建 tree 失败 %s: %s" % (st, tree))
    print("tree  :", tree["sha"])
    print("本地 tree:", c["tree"])
    if tree["sha"] != c["tree"]:
        print("⚠️  tree 不一致 —— 提交 SHA 将无法与本地相同")

    body = {
        "message": c["message"],
        "tree": tree["sha"],
        "parents": [remote_sha],
        "author": {"name": c["author"]["name"], "email": c["author"]["email"],
                   "date": iso(c["author"])},
        "committer": {"name": c["committer"]["name"],
                      "email": c["committer"]["email"],
                      "date": iso(c["committer"])},
    }
    st, commit = api("POST", "/repos/%s/%s/git/commits" % (OWNER, REPO), body)
    if st not in (200, 201):
        raise SystemExit("建 commit 失败 %s: %s" % (st, commit))
    remote_commit = commit["sha"]
    print("远端 commit:", remote_commit)
    print("本地 commit:", head)

    st, r = api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (OWNER, REPO, BRANCH),
                {"sha": remote_commit, "force": False})
    if st not in (200, 201):
        raise SystemExit("更新 ref 失败 %s: %s" % (st, r))
    print("✅ 远端 %s 已更新为 %s" % (BRANCH, r["object"]["sha"]))

    # ---------------------------------------------------------------
    # ★ 关键收尾：把远端那个 commit 对象**在本地重建出来**，再把本地 ref
    #   指过去，这样本地和远端 SHA 完全一致，下次 push 不会分叉。
    #
    #   实测 GitHub API 生成的 commit 与本地只差一处：
    #     **message 的末尾换行被去掉了**（时区其实原样保留 +0800，
    #       API JSON 里显示成 UTC 只是展示层归一化，别被误导）。
    #   所以穷举 0/1/2 个末尾换行，命中哪个就写哪个。
    # ---------------------------------------------------------------
    if remote_commit == head:
        print("✅ 本地远端 SHA 一致")
        return

    head_body = sh("git", "cat-file", "commit", head).encode()
    hdr, _, msg = head_body.partition(b"\n\n")
    import hashlib
    aligned = None
    for nz in (0, 1, 2):
        cand = hdr + b"\n\n" + msg.rstrip(b"\n") + b"\n" * nz
        if hashlib.sha1(b"commit %d\x00" % len(cand) + cand).hexdigest() == remote_commit:
            aligned = cand
            break
    if not aligned:
        print("\n⚠️ 无法在本地复现远端 commit 对象（GitHub 可能改动了别处）。")
        print("   本地 ref 未动 —— 下次 push 会 non-fast-forward，需人工对齐。")
        return
    subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                   input=aligned, check=True, capture_output=True)
    subprocess.run(["git", "update-ref", "refs/heads/%s" % BRANCH, remote_commit], check=True)
    now = sh("git", "rev-parse", "HEAD").strip()
    print("✅ 本地 %s 已对齐到 %s" % (BRANCH, now))
    print("   本地 == 远端 == %s，工作树干净，下次 push 不会分叉" % remote_commit)


if __name__ == "__main__":
    main()
