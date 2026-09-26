"""媒体音频提取器 —— 应用装配与业务逻辑。

职责划分：

* ``container.py``  — MP4 / MOV / M4A / MKV / WebM 的容器解析与**无损**抽取
* ``audiometa.py``  — WAV / FLAC / MP3 / OGG 的纯音频元数据
* ``ffmpeg.py``     — 可选转码引擎（检测到才可用）
* ``main.py``（本文件） — 路由、任务处理器、扫描缓存

设计要点：

* **绝不修改源文件。** 所有写操作都落在用户指定的输出目录里。
* 输出目录必须是白名单内的目录（与输入同样的路径校验），避免越权写。
* 扫描结果按 ``mtime + size`` 缓存，未变化的文件不重复探测（增量扫描）。
"""

import json
import os
import time

from tnasapp import fsapi, logx, server as srv

from . import audiometa, container, ffmpeg

APP_ID = "shh11-media-audio"
APP_VERSION = "1.0.1"
TITLE = "Media Audio Extractor"

#: 扫描时递归的最大深度，防止误选根目录后无限下钻
MAX_DEPTH = 24
#: 单次扫描最多收录的文件数（保护内存与任务时长）
MAX_FILES = 200000

MIGRATIONS = [
    # 版本 6（前 5 个版本是任务队列的 SCHEMA）
    """
    CREATE TABLE IF NOT EXISTS media_files (
        path        TEXT PRIMARY KEY,
        size        INTEGER NOT NULL DEFAULT 0,
        mtime       REAL    NOT NULL DEFAULT 0,
        family      TEXT,
        codec       TEXT,
        duration    REAL    NOT NULL DEFAULT 0,
        track_count INTEGER NOT NULL DEFAULT 0,
        audio_tracks TEXT,
        error       TEXT,
        probed_at   REAL    NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_media_family ON media_files(family)",
    """
    CREATE TABLE IF NOT EXISTS extract_results (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id     INTEGER,
        source     TEXT NOT NULL,
        output     TEXT,
        track_id   INTEGER,
        codec      TEXT,
        bytes      INTEGER NOT NULL DEFAULT 0,
        duration   REAL    NOT NULL DEFAULT 0,
        mode       TEXT NOT NULL DEFAULT 'stream-copy',
        ok         INTEGER NOT NULL DEFAULT 1,
        error      TEXT,
        created_at REAL    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_extract_job ON extract_results(job_id)",
]


def create_app(paths=None, log_level="INFO"):
    app = srv.App(
        APP_ID, TITLE, version=APP_VERSION, workers=2, log_level=log_level,
        extra_migrations=MIGRATIONS, paths=paths,
        description="把视频里的音轨无损抽取出来，支持批量处理。",
        engines={"ffmpeg": ffmpeg.status()},
    )
    fsapi.register(app)
    _register_routes(app)
    _register_jobs(app)
    return app


# ---------------------------------------------------------------- 工具


def _require_allowed(app, path, must_exist=True):
    """所有涉及用户文件的路径都必须过白名单。"""
    return app.allowed.check(path, must_exist=must_exist)


def _readable_file(app, path):
    real = _require_allowed(app, path)
    if not os.path.isfile(real):
        raise ValueError("不是文件：%s" % path)
    if not os.access(real, os.R_OK):
        raise PermissionError("没有读取权限：%s" % path)
    return real


def _walk_media(app, root, on_progress, ctx=None):
    """递归收集媒体文件。用 ``os.scandir`` 并跳过明显不该进的目录。"""
    found = []
    root = _require_allowed(app, root)
    if os.path.isfile(root):
        return [root] if os.path.splitext(root)[1].lower() in audiometa.MEDIA_EXTS else []

    stack = [(root, 0)]
    scanned = 0
    while stack:
        directory, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        if ctx is not None:
            ctx.checkpoint()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.startswith((".", "@")) or entry.name.startswith("$"):
                                continue
                            stack.append((entry.path, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext in audiometa.MEDIA_EXTS:
                                found.append(entry.path)
                                if len(found) >= MAX_FILES:
                                    return sorted(found)
                    except OSError:
                        continue
                    scanned += 1
                    if scanned % 500 == 0 and on_progress:
                        on_progress(scanned, len(found))
        except PermissionError:
            continue
        except OSError:
            continue
    return sorted(found)


def _cached_probe(app, path):
    """按 mtime + size 判断是否需要重新探测。"""
    try:
        stat = os.stat(path)
    except OSError as exc:
        return {"path": path, "error": "无法读取：%s" % exc}
    cached = app.store.query_one(
        "SELECT * FROM media_files WHERE path=?", (path,)
    )
    if cached and abs(cached["size"] - stat.st_size) < 1 and abs(cached["mtime"] - stat.st_mtime) < 1:
        return _row_to_probe(cached)

    probe = _probe_uncached(path)
    # 类型不受支持的文件**不入库** —— 用户随手点一个 .txt 不该污染媒体库
    # （这类错误只说明「不是媒体文件」，不是「媒体文件坏了」，后者才值得留痕）
    if probe.get("unsupported"):
        return probe

    app.store.execute(
        "INSERT INTO media_files (path,size,mtime,family,codec,duration,track_count,"
        "audio_tracks,error,probed_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
        "family=excluded.family, codec=excluded.codec, duration=excluded.duration,"
        "track_count=excluded.track_count, audio_tracks=excluded.audio_tracks,"
        "error=excluded.error, probed_at=excluded.probed_at",
        (
            path, stat.st_size, stat.st_mtime,
            probe.get("family"), probe.get("codec"), float(probe.get("duration") or 0),
            int(probe.get("track_count") or 0),
            json.dumps(probe.get("audio_tracks") or [], ensure_ascii=False),
            probe.get("error"), time.time(),
        ),
    )
    return probe


def _row_to_probe(row):
    try:
        tracks = json.loads(row["audio_tracks"] or "[]")
    except ValueError:
        tracks = []
    return {
        "path": row["path"], "size": row["size"], "family": row["family"],
        "codec": row["codec"], "duration": row["duration"],
        "track_count": row["track_count"], "audio_tracks": tracks,
        "error": row["error"], "cached": True,
    }


def _probe_uncached(path):
    """探测一个文件；纯音频与容器化媒体统一到这里。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in audiometa.READERS:
            meta = audiometa.read_audio_metadata(path)
            track = {
                "track_id": 1, "kind": "audio", "codec": meta.get("codec"),
                "codec_name": meta.get("codec"), "language": "und",
                "channels": meta.get("channels"), "sample_rate": meta.get("sample_rate"),
                "bit_depth": meta.get("bit_depth"), "duration": meta.get("duration") or 0,
                "extractable": False,
            }
            return {
                "path": path, "size": meta.get("size"), "family": "raw-audio",
                "codec": meta.get("codec"), "duration": meta.get("duration") or 0,
                "track_count": 1, "audio_tracks": [track], "tags": meta.get("tags") or {},
                "error": None,
            }
        info = container.probe(path)
        return {
            "path": path, "size": info["size"], "family": info["family"],
            "codec": (info["audio_tracks"][0]["codec"] if info["audio_tracks"] else None),
            "duration": info["duration"], "track_count": len(info["tracks"]),
            "audio_tracks": info["audio_tracks"], "tracks": info["tracks"],
            "brand": info.get("brand"), "error": None,
        }
    except container.UnsupportedFormat as exc:
        # 不是媒体文件：标 unsupported，调用方据此决定不写进媒体库
        return {"path": path, "error": str(exc), "unsupported": True}
    except (container.ContainerError, OSError) as exc:
        return {"path": path, "error": "解析失败：%s" % exc}


def _pick_track(probe, requested):
    """选轨道。``requested`` 为空或 'auto' 时取第一条可抽取的音轨。"""
    candidates = [t for t in (probe.get("audio_tracks") or []) if t.get("extractable")]
    if not candidates:
        return None
    if requested in (None, "", "auto"):
        return candidates[0]
    try:
        wanted = int(requested)
    except (TypeError, ValueError):
        return candidates[0]
    for track in candidates:
        if track.get("track_id") == wanted:
            return track
    return None


def _unique_output(app, path, output_dir, track, transcode):
    """输出路径：``<output_dir>/<stem>.<ext>``，重名时加 -1、-2 后缀。

    只改**文件名**，不覆盖已有文件（除非调用方明确要求覆盖）。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    if len(stem) > 120:
        stem = stem[:120]
    if transcode and ffmpeg.PRESETS.get(transcode):
        ext = ffmpeg.PRESETS[transcode]["ext"]
    else:
        family = container.family_for(path) or container.sniff(path) or "iso-bmff"
        ext = ".mka" if family == "ebml" else ".m4a"
    suffix = ".t%s" % track["track_id"] if track.get("track_id") else ""
    candidate = os.path.join(output_dir, stem + suffix + ext)
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(output_dir, "%s%s-%d%s" % (stem, suffix, index, ext))
        index += 1
        if index > 999:
            break
    return candidate


# ---------------------------------------------------------------- 路由


def _register_routes(app):
    @app.get("/api/engines")
    def _engines(req):
        return srv.Response.json({
            "ok": True,
            "engines": {name: engine for name, engine in app.engines.items()},
            "engine_detail": ffmpeg.status(),
            "presets": ffmpeg.preset_list(),
        })

    @app.get("/api/media/probe")
    def _probe(req):
        path = req.arg("path", "")
        try:
            real = _require_allowed(app, path)
        except Exception as exc:
            return srv.Response.error(str(exc), 403,
                                      "请先在「设置」里把该目录加入可访问目录")
        if not os.path.isfile(real):
            return srv.Response.error("不是文件：%s" % path, 400)
        result = _cached_probe(app, real)
        result["ok"] = result.get("error") is None
        return srv.Response.json(result)

    @app.get("/api/media/files")
    def _files(req):
        limit = max(1, min(500, req.int_arg("limit", 100)))
        offset = max(0, req.int_arg("offset", 0))
        keyword = (req.arg("q") or "").strip()
        with_audio = req.bool_arg("with_audio")
        clauses, params = [], []
        if keyword:
            clauses.append("path LIKE ?")
            params.append("%" + keyword + "%")
        if with_audio:
            clauses.append("(audio_tracks IS NOT NULL AND audio_tracks != '[]')")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = app.store.query(
            "SELECT * FROM media_files%s ORDER BY path LIMIT ? OFFSET ?" % where,
            tuple(params + [limit, offset]),
        )
        total = app.store.scalar("SELECT COUNT(*) FROM media_files%s" % where,
                                 tuple(params), default=0)
        return srv.Response.json({
            "ok": True,
            "total": total,
            "files": [_row_to_probe(row) for row in rows],
        })

    @app.get("/api/media/results")
    def _results(req):
        job_id = req.arg("job_id")
        if job_id:
            rows = app.store.query(
                "SELECT * FROM extract_results WHERE job_id=? ORDER BY id",
                (int(job_id),),
            )
        else:
            rows = app.store.query(
                "SELECT * FROM extract_results ORDER BY id DESC LIMIT ?",
                (max(1, min(500, req.int_arg("limit", 100))),),
            )
        return srv.Response.json({"ok": True, "results": rows})

    @app.get("/api/media/summary")
    def _summary(req):
        total = app.store.scalar("SELECT COUNT(*) FROM media_files", default=0)
        with_audio = app.store.scalar(
            "SELECT COUNT(*) FROM media_files WHERE audio_tracks IS NOT NULL "
            "AND audio_tracks != '[]'", default=0,
        )
        failed = app.store.scalar(
            "SELECT COUNT(*) FROM media_files WHERE error IS NOT NULL", default=0,
        )
        size = app.store.scalar("SELECT COALESCE(SUM(size),0) FROM media_files", default=0)
        families = app.store.query(
            "SELECT family, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes "
            "FROM media_files GROUP BY family ORDER BY n DESC"
        )
        extracted = app.store.scalar(
            "SELECT COUNT(*) FROM extract_results WHERE ok=1", default=0,
        )
        saved = app.store.scalar(
            "SELECT COALESCE(SUM(bytes),0) FROM extract_results WHERE ok=1", default=0,
        )
        return srv.Response.json({
            "ok": True,
            "total": total, "with_audio": with_audio, "failed": failed,
            "total_bytes": size, "families": families,
            "extracted": extracted, "extracted_bytes": saved,
        })

    @app.get("/api/media/detect")
    def _detect(req):
        """快速判断一个路径是文件还是目录，并给出可用操作。"""
        path = req.arg("path", "")
        try:
            real = _require_allowed(app, path)
        except Exception as exc:
            return srv.Response.error(str(exc), 403)
        ext = os.path.splitext(real)[1].lower()
        return srv.Response.json({
            "ok": True, "path": real,
            "is_dir": os.path.isdir(real),
            "supported": ext in audiometa.MEDIA_EXTS,
            "extension": ext,
        })


# ---------------------------------------------------------------- 任务


def _register_jobs(app):
    @app.jobs.register("scan")
    def _job_scan(ctx):
        roots = ctx.params.get("roots") or []
        if not roots:
            raise ValueError("没有指定要扫描的目录")
        ctx.log("开始扫描 %d 个位置" % len(roots))
        files = []
        for index, root in enumerate(roots, 1):
            ctx.message("正在收集：%s" % root)
            files.extend(_walk_media(app, root, None, ctx))
            ctx.progress(index, len(roots), "已找到 %d 个媒体文件" % len(files))

        total = len(files)
        ctx.log("共找到 %d 个媒体文件，开始解析" % total)
        ok = failed = skipped = 0
        for index, path in enumerate(files, 1):
            ctx.checkpoint()
            result = _cached_probe(app, path)
            if result.get("cached"):
                skipped += 1
            elif result.get("error"):
                failed += 1
                ctx.log("解析失败：%s —— %s" % (path, result["error"]), "WARN")
            else:
                ok += 1
            if index % 10 == 0 or index == total:
                ctx.progress(index, total, "已解析 %d/%d" % (index, total))

        ctx.set_result({"found": total, "parsed": ok, "failed": failed,
                        "from_cache": skipped})

    @app.jobs.register("extract")
    def _job_extract(ctx):
        params = ctx.params
        output_dir = params.get("output_dir")
        if not output_dir:
            raise ValueError("没有指定输出目录")
        out_dir = app.allowed.check(output_dir, must_exist=False)
        os.makedirs(out_dir, exist_ok=True)
        if not os.access(out_dir, os.W_OK):
            raise PermissionError("输出目录不可写：%s" % output_dir)

        requested_track = params.get("track_id") or "auto"
        preset = params.get("preset") or None
        transcode = preset if preset not in ("", "none", None) else None
        if transcode and not ffmpeg.detect()["available"]:
            raise RuntimeError("选择了转码预设，但系统上没有可用的 ffmpeg")

        inputs = []
        for root in params.get("roots") or []:
            inputs.extend(_walk_media(app, root, None, ctx))
        for path in params.get("inputs") or []:
            real = _require_allowed(app, path)
            if os.path.isfile(real):
                inputs.append(real)
        inputs = sorted(set(inputs))
        if not inputs:
            raise ValueError("没有找到可处理的媒体文件")

        ctx.log("待处理 %d 个文件，输出到 %s" % (len(inputs), out_dir))
        results = []
        done = 0
        for index, path in enumerate(inputs, 1):
            ctx.checkpoint()
            ctx.message("处理 %s" % os.path.basename(path))
            record = _extract_one(app, ctx, path, out_dir, requested_track, transcode)
            results.append(record)
            if record["ok"]:
                done += 1
            else:
                ctx.log("失败：%s —— %s" % (path, record["error"]), "WARN")
            ctx.progress(index, len(inputs),
                         "%d/%d 成功 %d" % (index, len(inputs), done))
            app.store.execute(
                "INSERT INTO extract_results (job_id,source,output,track_id,codec,bytes,"
                "duration,mode,ok,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ctx.job_id, path, record.get("output"), record.get("track_id"),
                 record.get("codec"), int(record.get("bytes") or 0),
                 float(record.get("duration") or 0), record.get("mode") or "",
                 1 if record["ok"] else 0, record.get("error"), time.time()),
            )
        ctx.set_result({"total": len(inputs), "succeeded": done,
                        "failed": len(inputs) - done, "output_dir": out_dir})


def _extract_one(app, ctx, path, out_dir, requested_track, transcode):
    """处理单个文件：探测 → 选轨 → 无损抽取或转码。"""
    probe = _cached_probe(app, path)
    if probe.get("error"):
        return {"ok": False, "error": probe["error"], "source": path}

    track = _pick_track(probe, requested_track)
    if track is None:
        return {"ok": False, "source": path,
                "error": "没有可抽取的音轨（文件里没有音轨，或缺少索引表）"}

    target = _unique_output(app, path, out_dir, track, transcode)
    try:
        if transcode:
            result = ffmpeg.transcode(path, target, transcode)
            mode = "transcode:%s" % transcode
        else:
            if not track.get("extractable"):
                return {"ok": False, "source": path,
                        "error": "该格式无法无损抽取（可改用转码预设）"}
            result = container.extract(
                path, track["track_id"], target,
                progress=lambda done, total: ctx.progress(done, total, "复制音轨数据"),
            )
            mode = "stream-copy"
    except (container.ContainerError, ffmpeg.TranscodeError, OSError) as exc:
        return {"ok": False, "source": path, "error": str(exc), "track_id": track.get("track_id")}

    ctx.log("已输出 %s（%s，%.1f MB）"
            % (os.path.basename(target), mode, result["bytes"] / 1048576))
    return {
        "ok": True, "source": path, "output": target,
        "track_id": track.get("track_id"), "codec": result.get("codec"),
        "bytes": result["bytes"], "duration": track.get("duration") or 0,
        "mode": mode,
    }


def main(argv=None):
    from tnasapp import cli

    return cli.main(APP_ID, APP_VERSION, create_app, argv=argv,
                    description="媒体音频提取器 —— 把视频里的音轨无损抽出来")
