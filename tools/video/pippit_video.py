"""小云雀 (Pippit) 视频生成工具 — 字节跳动剪映官方直连 API。

基于 Seedance 2.0 系列模型,支持:
- 营销视频生成(自然语言指令 + 商品图 -> 种草短视频)
- 沉浸式短片生成(支持首尾帧、参考图/视频/音频)
- 文件上传(素材管理,返回 pippit_asset_id)
- 结果查询(轮询任务状态并下载视频)

认证:Authorization: Bearer <PIPPIT_ACCESS_KEY>
官方文档:https://bytedance.larkoffice.com/docx/CQOYdJNLioLz6fxRzKXcCsKLnJh
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    DependencyError,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.video._shared import probe_output

# 小云雀 API 基址
_BASE_URL = "https://xyq.jianying.com/api/biz/v1"

# 端点路径
_EP_UPLOAD = f"{_BASE_URL}/skill/upload_file"
_EP_MARKETING = f"{_BASE_URL}/agent/submit_marketing_run"
_EP_IMMERSIVE = f"{_BASE_URL}/skill/submit_run"
_EP_QUERY = f"{_BASE_URL}/agent/query_generate_video_result"

# 任务状态枚举
_STATE_CREATED = 1
_STATE_PROCESSING = 2
_STATE_SUCCESS = 3
_STATE_FAILED = 4
_STATE_CANCELLED = 5

# 营销视频 ratio 枚举(int)
_RATIO_MARKETING = {"16:9": 2, "9:16": 3, "4:3": 4, "3:4": 5, "1:1": 6}

# 可选模型列表
_MODELS = [
    "seedance2.0_fast_vision",  # VIP,快速
    "seedance2.0_vision",       # VIP,高质量,支持1080p
    "Seedance_2.0_mini",        # VIP,轻量
    "Seedance_2.0_mini_lite",   # 非VIP
]


class PippitVideo(BaseTool):
    """小云雀视频生成工具。"""

    name = "pippit_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "pippit"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:PIPPIT_ACCESS_KEY"]
    install_instructions = (
        "Set PIPPIT_ACCESS_KEY to your 小云雀 (Pippit) Access Key.\n"
        "  申请方式:登录 https://xyq.jianying.com/home -> 顶部【CLI/API】-> 【API】-> 新建秘钥"
    )
    agent_skills = ["seedance-2-0", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video", "marketing_video", "first_last_frame"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "marketing_video": True,
        "first_last_frame": True,
        "reference_image": True,
        "reference_video": True,
        "reference_audio": True,
        "native_audio": True,
        "aspect_ratio": True,
        "subtitle_overlay": True,  # show_subtitle 参数
    }
    best_for = [
        "字节官方直连的 Seedance 2.0 视频生成(无需第三方网关)",
        "营销种草短视频(商品图 + 自然语言指令 -> 社媒投放视频)",
        "沉浸式短片(支持首尾帧模式、参考图/视频/音频)",
        "中文场景与中文指令理解(原生中文服务)",
    ]
    not_good_for = ["无 PIPPIT_ACCESS_KEY 的环境", "纯本地离线生成"]
    fallback_tools = ["seedance_video", "seedance_replicate", "kling_video", "veo_video"]
    quality_score = 0.92

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "自然语言创作指令(中文为佳)。营销视频必填;沉浸式短片与 message 保持一致。",
            },
            "operation": {
                "type": "string",
                "enum": ["generate", "marketing", "immersive", "upload", "query"],
                "default": "generate",
                "description": (
                    "generate=完整流程(提交+轮询+下载,默认);"
                    "marketing=仅提交营销视频任务;immersive=仅提交沉浸式短片;"
                    "upload=上传素材;query=查询任务结果"
                ),
            },
            "video_type": {
                "type": "string",
                "enum": ["marketing", "immersive"],
                "default": "immersive",
                "description": "generate 操作下选择视频类型:营销 or 沉浸式短片",
            },
            "model": {
                "type": "string",
                "enum": _MODELS,
                "default": "Seedance_2.0_mini",
                "description": "VIP 模型(seedance2.0_fast_vision/vision/Seedance_2.0_mini)或非VIP(Seedance_2.0_mini_lite)",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "4:3", "3:4", "1:1"],
                "default": "16:9",
            },
            "resolution": {
                "type": "string",
                "enum": ["480p", "720p", "1080p"],
                "default": "720p",
                "description": "1080p 目前仅支持 seedance2.0_vision 模型",
            },
            "duration_sec": {
                "type": "integer",
                "minimum": 1,
                "default": 10,
                "description": "期望视频时长(秒)。沉浸式短片必填;营销视频用 duration_start/duration_end",
            },
            "duration_start": {
                "type": "integer",
                "description": "营销视频时长下限(秒),与 duration_end 配合使用",
            },
            "duration_end": {
                "type": "integer",
                "description": "营销视频时长上限(秒);精确时长时与 duration_start 传同一值",
            },
            "show_subtitle": {
                "type": "boolean",
                "default": False,
                "description": "营销视频是否展示字幕",
            },
            "first_last_frame": {
                "type": "boolean",
                "default": False,
                "description": "沉浸式短片是否启用首尾帧模式(generate_type=1)",
            },
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "本地参考图片路径列表,会自动上传获取 asset_id",
            },
            "image_asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "已上传的图片 asset_id 列表(跳过上传步骤)",
            },
            "video_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "沉浸式短片参考视频本地路径,会自动上传",
            },
            "audio_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "沉浸式短片参考音频本地路径,会自动上传",
            },
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "营销视频的素材 ID 列表(已上传的 asset_id)",
            },
            "upload_file_path": {
                "type": "string",
                "description": "upload 操作的本地文件路径",
            },
            "thread_id": {"type": "string", "description": "复用已有会话;query 操作必填"},
            "run_id": {"type": "string", "description": "query 操作必填,来自提交任务返回"},
            "poll_interval": {
                "type": "integer",
                "minimum": 3,
                "default": 8,
                "description": "轮询间隔(秒)",
            },
            "poll_timeout": {
                "type": "integer",
                "minimum": 30,
                "default": 600,
                "description": "轮询超时(秒)",
            },
            "output_path": {"type": "string", "description": "视频下载保存路径"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2, retryable_errors=["rate_limit", "timeout", "connection_error"]
    )
    idempotency_key_fields = ["prompt", "operation", "model", "aspect_ratio", "duration_sec"]
    side_effects = [
        "writes video file to output_path",
        "calls 小云雀 API (xyq.jianying.com)",
        "may upload local files to 小云雀 asset storage",
    ]
    user_visible_verification = [
        "Watch generated clip for motion coherence and audio sync",
        "Confirm video duration matches the requested duration_sec",
    ]

    def _get_api_key(self) -> str | None:
        return os.environ.get("PIPPIT_ACCESS_KEY")

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if self._get_api_key() else ToolStatus.UNAVAILABLE

    def check_dependencies(self) -> None:
        # 重写以使用 PIPPIT_ACCESS_KEY 而非 dependencies 列表中的通用检查
        if not self._get_api_key():
            raise DependencyError(self.install_instructions)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        # 小云雀视频生成通常 1-5 分钟,按时长和模型粗估
        duration = inputs.get("duration_sec", 10)
        model = inputs.get("model", "Seedance_2.0_mini")
        # fast 模型更快,vision 模型更慢
        if "fast" in model:
            return max(60.0, duration * 8)
        if "vision" in model and "fast" not in model:
            return max(180.0, duration * 15)
        return max(120.0, duration * 10)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """根据 operation 路由到对应的处理逻辑。"""
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="PIPPIT_ACCESS_KEY not set. " + self.install_instructions,
            )

        operation = inputs.get("operation", "generate")
        start = time.time()

        try:
            if operation == "upload":
                return self._do_upload(inputs, api_key, start)
            if operation == "query":
                return self._do_query(inputs, api_key, start)
            if operation == "marketing":
                return self._do_marketing(inputs, api_key, start, poll=False)
            if operation == "immersive":
                return self._do_immersive(inputs, api_key, start, poll=False)
            # 默认 generate:完整流程(提交 + 轮询 + 下载)
            return self._do_generate(inputs, api_key, start)
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"小云雀视频生成失败({operation}):{e}",
                duration_seconds=round(time.time() - start, 2),
            )

    # ---- 底层操作 ----

    def _do_upload(self, inputs: dict[str, Any], api_key: str, start: float) -> ToolResult:
        """上传文件到小云雀素材库。"""
        file_path = inputs.get("upload_file_path") or inputs.get("image_paths", [None])[0]
        if not file_path:
            return ToolResult(success=False, error="upload 操作需要 upload_file_path 或 image_paths[0]")

        asset_id = self._upload_file(file_path, api_key)
        return ToolResult(
            success=True,
            data={
                "provider": "pippit",
                "operation": "upload",
                "pippit_asset_id": asset_id,
                "source_file": str(file_path),
            },
            artifacts=[],
            duration_seconds=round(time.time() - start, 2),
        )

    def _do_marketing(
        self, inputs: dict[str, Any], api_key: str, start: float, *, poll: bool
    ) -> ToolResult:
        """提交营销视频任务。poll=True 时会轮询并下载视频。"""
        prompt = inputs["prompt"]
        settings: dict[str, Any] = {}

        ratio_str = inputs.get("aspect_ratio", "16:9")
        if ratio_str in _RATIO_MARKETING:
            settings["ratio"] = _RATIO_MARKETING[ratio_str]
        if inputs.get("model"):
            settings["video_model"] = inputs["model"]
        if inputs.get("duration_start") is not None:
            settings["duration_start"] = inputs["duration_start"]
        if inputs.get("duration_end") is not None:
            settings["duration_end"] = inputs["duration_end"]
        if "show_subtitle" in inputs:
            settings["show_subtitle"] = inputs["show_subtitle"]
        if inputs.get("resolution"):
            settings["video_resolution"] = inputs["resolution"]

        payload: dict[str, Any] = {"message": prompt, "general_agent_settings": settings}
        if inputs.get("asset_ids"):
            payload["asset_ids"] = inputs["asset_ids"]
        if inputs.get("thread_id"):
            payload["thread_id"] = inputs["thread_id"]

        run_info = self._submit_task(_EP_MARKETING, payload, api_key)

        if not poll:
            return self._run_info_result(run_info, "marketing", start)

        return self._poll_and_download(inputs, api_key, run_info, "marketing", start)

    def _do_immersive(
        self, inputs: dict[str, Any], api_key: str, start: float, *, poll: bool
    ) -> ToolResult:
        """提交沉浸式短片任务。poll=True 时会轮询并下载视频。"""
        prompt = inputs["prompt"]

        # 收集参考素材:本地路径自动上传,asset_id 直接使用
        image_asset_ids = list(inputs.get("image_asset_ids") or [])
        for img_path in inputs.get("image_paths") or []:
            image_asset_ids.append({"pippit_asset_id": self._upload_file(img_path, api_key)})

        video_refs = []
        for vid_path in inputs.get("video_paths") or []:
            video_refs.append({"pippit_asset_id": self._upload_file(vid_path, api_key)})

        audio_refs = []
        for aud_path in inputs.get("audio_paths") or []:
            audio_refs.append({"pippit_asset_id": self._upload_file(aud_path, api_key)})

        tool_param: dict[str, Any] = {
            "prompt": prompt,
            "model": inputs.get("model", "Seedance_2.0_mini"),
            "duration_sec": inputs.get("duration_sec", 10),
        }
        if inputs.get("aspect_ratio"):
            # 沉浸式短片的 ratio 是字符串
            tool_param["ratio"] = inputs["aspect_ratio"]
        if inputs.get("resolution"):
            tool_param["resolution"] = inputs["resolution"]
        if inputs.get("first_last_frame"):
            tool_param["generate_type"] = 1  # 首尾帧模式
        if image_asset_ids:
            tool_param["images"] = image_asset_ids
        if video_refs:
            tool_param["videos"] = video_refs
        if audio_refs:
            tool_param["audios"] = audio_refs

        payload: dict[str, Any] = {
            "message": prompt,
            "agent_name": "pippit_video_part_agent",
            "video_part_tool_param": tool_param,
        }
        if inputs.get("asset_ids"):
            payload["asset_ids"] = inputs["asset_ids"]
        if inputs.get("thread_id"):
            payload["thread_id"] = inputs["thread_id"]

        run_info = self._submit_task(_EP_IMMERSIVE, payload, api_key)

        if not poll:
            return self._run_info_result(run_info, "immersive", start)

        return self._poll_and_download(inputs, api_key, run_info, "immersive", start)

    def _do_query(self, inputs: dict[str, Any], api_key: str, start: float) -> ToolResult:
        """查询任务结果(不轮询,只查一次)。"""
        thread_id = inputs.get("thread_id")
        run_id = inputs.get("run_id")
        if not thread_id or not run_id:
            return ToolResult(
                success=False,
                error="query 操作需要 thread_id 和 run_id",
            )

        data = self._query_run(thread_id, run_id, api_key)
        state = int(data.get("run_state", 0))
        result_data: dict[str, Any] = {
            "provider": "pippit",
            "operation": "query",
            "thread_id": thread_id,
            "run_id": run_id,
            "run_state": state,
            "state_description": self._state_desc(state),
            "video_urls": data.get("video_urls") or [],
            "image_urls": data.get("image_urls") or [],
        }
        if data.get("fail_reason"):
            result_data["fail_reason"] = data["fail_reason"]

        # 若已完成且有视频链接,且指定了 output_path,则下载
        if state == _STATE_SUCCESS and data.get("video_urls") and inputs.get("output_path"):
            self._download_first(data["video_urls"], inputs["output_path"])

        return ToolResult(
            success=True,
            data=result_data,
            duration_seconds=round(time.time() - start, 2),
        )

    def _do_generate(self, inputs: dict[str, Any], api_key: str, start: float) -> ToolResult:
        """完整生成流程:提交 + 轮询 + 下载。"""
        video_type = inputs.get("video_type", "immersive")
        if video_type == "marketing":
            return self._do_marketing(inputs, api_key, start, poll=True)
        return self._do_immersive(inputs, api_key, start, poll=True)

    # ---- HTTP 交互层(每个函数只做一件事) ----

    def _upload_file(self, file_path: str, api_key: str) -> str:
        """上传本地文件,返回 pippit_asset_id。"""
        import requests

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"上传文件不存在:{file_path}")

        with path.open("rb") as f:
            resp = requests.post(
                _EP_UPLOAD,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                },
                files={"file": (path.name, f)},
                timeout=120,
            )
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("ret", "1")) != "0":
            raise RuntimeError(f"上传失败:{body.get('errmsg', '未知错误')} (log_id={body.get('log_id')})")
        return body["data"]["pippit_asset_id"]

    def _submit_task(self, endpoint: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        """提交任务,返回 run 信息(run_id, thread_id, state, web_thread_link)。"""
        import requests

        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("ret", "1")) != "0":
            raise RuntimeError(
                f"提交任务失败:{body.get('errmsg', '未知错误')} (log_id={body.get('log_id')})"
            )
        return body["data"]

    def _query_run(self, thread_id: str, run_id: str, api_key: str) -> dict[str, Any]:
        """查询任务结果,返回 data 字段。"""
        import requests

        resp = requests.post(
            _EP_QUERY,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"thread_id": thread_id, "run_id": run_id},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("ret", "1")) != "0":
            raise RuntimeError(
                f"查询失败:{body.get('errmsg', '未知错误')} (log_id={body.get('log_id')})"
            )
        return body.get("data", {})

    def _poll_and_download(
        self,
        inputs: dict[str, Any],
        api_key: str,
        run_info: dict[str, Any],
        video_type: str,
        start: float,
    ) -> ToolResult:
        """轮询任务直到完成,然后下载视频。"""
        run_data = run_info.get("run", run_info)
        thread_id = run_data["thread_id"]
        run_id = run_data["run_id"]

        poll_interval = inputs.get("poll_interval", 8)
        poll_timeout = inputs.get("poll_timeout", 600)
        deadline = time.time() + poll_timeout

        final_state = _STATE_PROCESSING
        query_data: dict[str, Any] = {}
        while time.time() < deadline:
            query_data = self._query_run(thread_id, run_id, api_key)
            final_state = int(query_data.get("run_state", 0))
            if final_state in (_STATE_SUCCESS, _STATE_FAILED, _STATE_CANCELLED):
                break
            time.sleep(poll_interval)

        if final_state != _STATE_SUCCESS:
            fail_reason = query_data.get("fail_reason", {})
            return ToolResult(
                success=False,
                error=(
                    f"任务未成功完成(state={self._state_desc(final_state)});"
                    f"fail_reason={fail_reason};thread_id={thread_id};run_id={run_id}"
                ),
                data={
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "run_state": final_state,
                    "fail_reason": fail_reason,
                    "web_thread_link": run_info.get("web_thread_link"),
                },
                duration_seconds=round(time.time() - start, 2),
            )

        video_urls = query_data.get("video_urls") or []
        if not video_urls:
            return ToolResult(
                success=False,
                error=f"任务成功但未返回视频链接;thread_id={thread_id};run_id={run_id}",
                data={
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "image_urls": query_data.get("image_urls") or [],
                    "web_thread_link": run_info.get("web_thread_link"),
                },
                duration_seconds=round(time.time() - start, 2),
            )

        output_path = Path(inputs.get("output_path", "pippit_output.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._download_first(video_urls, str(output_path))

        return ToolResult(
            success=True,
            data={
                "provider": "pippit",
                "model": inputs.get("model", "Seedance_2.0_mini"),
                "video_type": video_type,
                "prompt": inputs.get("prompt"),
                "aspect_ratio": inputs.get("aspect_ratio", "16:9"),
                "resolution": inputs.get("resolution", "720p"),
                "thread_id": thread_id,
                "run_id": run_id,
                "web_thread_link": run_info.get("web_thread_link"),
                "video_urls": video_urls,
                "image_urls": query_data.get("image_urls") or [],
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                **probe_output(output_path),
            },
            artifacts=[str(output_path)],
            duration_seconds=round(time.time() - start, 2),
            model=inputs.get("model", "Seedance_2.0_mini"),
        )

    def _download_first(self, video_urls: list[str], output_path: str) -> None:
        """下载第一个视频链接到本地。"""
        import requests

        if not video_urls:
            raise RuntimeError("没有可下载的视频链接")
        resp = requests.get(video_urls[0], timeout=180)
        resp.raise_for_status()
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)

    def _run_info_result(
        self, run_info: dict[str, Any], video_type: str, start: float
    ) -> ToolResult:
        """构造"仅提交未轮询"的返回结果。"""
        run_data = run_info.get("run", run_info)
        return ToolResult(
            success=True,
            data={
                "provider": "pippit",
                "operation": "submit_only",
                "video_type": video_type,
                "thread_id": run_data.get("thread_id"),
                "run_id": run_data.get("run_id"),
                "state": run_data.get("state"),
                "web_thread_link": run_info.get("web_thread_link"),
                "hint": "任务已提交,使用 operation=query 或 run_id 轮询结果",
            },
            duration_seconds=round(time.time() - start, 2),
        )

    @staticmethod
    def _state_desc(state: int) -> str:
        """任务状态码转中文描述。"""
        return {
            _STATE_CREATED: "已创建",
            _STATE_PROCESSING: "处理中",
            _STATE_SUCCESS: "成功",
            _STATE_FAILED: "失败",
            _STATE_CANCELLED: "已取消",
        }.get(state, f"未知({state})")
