import json
import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import web_app
from ai_agent import AgentManager, validate_arguments


SETTINGS = {
    "api_base_url": "http://127.0.0.1:1/v1", "model": "test-tools",
    "api_key": "test-private-key", "library_dir": "/tmp/walkman-test-library",
    "bilibili_cookies": "",
}


def tool(name, args, call_id="call_test"):
    return {"content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)},
    }]}


def wait_job(manager, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = manager.snapshot(job["id"])
        if result["status"] in {"completed", "failed", "needs_input", "cancelled"}:
            return result
        time.sleep(0.01)
    raise AssertionError("background job did not finish")


class MusicAgentTests(unittest.TestCase):
    def manager(self, responses, execute=None):
        chat = Mock(side_effect=responses)
        execute = execute if execute is not None else Mock(return_value={"ok": True, "output": "完成"})
        return AgentManager(lambda: dict(SETTINGS), chat, execute), chat, execute

    def test_sentence_searches_then_downloads_with_real_result_ids(self):
        calls = []

        def action(name, args, apply, **kwargs):
            calls.append((name, dict(args), apply))
            if name == "search":
                return {"ok": True, "data": {"tracks": [{"id": 185868, "name": "First Love"}]}, "output": "找到 1 首"}
            return {"ok": True, "code": 0, "output": "下载成功"}

        manager, chat, _ = self.manager([
            tool("search", {"query": "宇多田光 First Love"}),
            tool("download", {"track_ids": ["185868"], "by_album": True}),
            {"content": "已下载到默认音乐目录。"},
        ], web_app.run_agent_tool)
        with patch("web_app.run_action", side_effect=action):
            result = wait_job(manager, manager.start("帮我找宇多田光 First Love 原版并下载"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call[0] for call in calls], ["search", "download"])
        self.assertTrue(all(call[2] for call in calls))
        self.assertEqual(calls[1][1]["track_ids"], ["185868"])
        self.assertEqual(calls[1][1]["output_dir"], SETTINGS["library_dir"])
        history = chat.call_args.args[1]
        responses = [json.loads(message["content"]) for message in history if message["role"] == "tool"]
        self.assertEqual(responses[0]["data"]["tracks"][0]["id"], 185868)
        self.assertEqual(len({message["tool_call_id"] for message in history if message["role"] == "tool"}), 2)

    def test_unverified_song_id_cannot_launch_download(self):
        manager, _, _ = self.manager([
            tool("download", {"track_ids": ["99999999"]}),
            {"content": "需要先搜索歌曲。"},
        ], web_app.run_agent_tool)
        with patch("web_app.run_action") as action:
            result = wait_job(manager, manager.start("下载 First Love"))
        action.assert_not_called()
        self.assertTrue(any(not event["ok"] for event in result["events"] if event["type"] == "tool_finished"))

    def test_followup_resumes_original_task_context(self):
        manager, chat, execute = self.manager([
            tool("ask_user", {"question": "来源文件夹在哪里？"}),
            tool("convert", {"source": "/tmp/music", "output_dir": "/tmp/converted"}),
            {"content": "转换完成。"},
        ])
        first = wait_job(manager, manager.start("转换我的 NCM 文件"))
        self.assertEqual(first["status"], "needs_input")
        second = wait_job(manager, manager.start("来源是 /tmp/music", first["session_id"]))
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["status"], "completed")
        user_messages = [message["content"] for message in chat.call_args.args[1] if message["role"] == "user"]
        self.assertEqual(user_messages, ["转换我的 NCM 文件", "来源是 /tmp/music"])
        execute.assert_called_once()

    def test_duplicate_write_is_not_executed_twice(self):
        manager, chat, execute = self.manager([
            tool("repair", {}),
            tool("repair", {}),
            {"content": "歌词修复完成。"},
        ])
        result = wait_job(manager, manager.start("修复歌词"))
        self.assertEqual(result["status"], "completed")
        execute.assert_called_once()
        last_tool = [message for message in chat.call_args.args[1] if message["role"] == "tool"][-1]
        self.assertTrue(json.loads(last_tool["content"])["already_executed"])

    def test_failed_write_is_not_retried_implicitly(self):
        execute = Mock(side_effect=RuntimeError("磁盘已满"))
        manager, chat, _ = self.manager([tool("repair", {}), tool("repair", {}), {"content": "磁盘已满，任务未完成。"}], execute)
        result = wait_job(manager, manager.start("修复歌词"))
        execute.assert_called_once()
        self.assertIn("磁盘已满", result["result"])
        self.assertFalse(json.loads([m for m in chat.call_args.args[1] if m["role"] == "tool"][-1]["content"])["ok"])

    def test_read_only_request_cannot_write_even_if_model_tries(self):
        manager, _, execute = self.manager([tool("repair", {}), {"content": "只查看，没有修改。"}])
        result = wait_job(manager, manager.start("只查看默认曲库，不修改"))
        execute.assert_not_called()
        self.assertEqual(result["status"], "completed")

    def test_invalid_or_unknown_tools_never_reach_executor(self):
        for response in [tool("shell", {"command": "anything"}), tool("download", {"track_ids": "1234"}),
                         tool("repair", {"root": "/tmp/music", "overwrite": True})]:
            with self.subTest(response=response):
                manager, _, execute = self.manager([response, {"content": "工具参数错误。"}])
                wait_job(manager, manager.start("整理音乐"))
                execute.assert_not_called()

    def test_secret_is_redacted_from_progress_and_model_tool_results(self):
        def execute(name, args, settings, context, cancel, progress):
            progress("log: " + settings["api_key"])
            return {"ok": False, "output": settings["api_key"]}

        manager, chat, _ = self.manager([tool("environment", {}), {"content": "完成。"}], execute)
        result = wait_job(manager, manager.start("检查环境"))
        self.assertNotIn(SETTINGS["api_key"], json.dumps(result))
        tool_content = [m["content"] for m in chat.call_args.args[1] if m["role"] == "tool"]
        self.assertNotIn(SETTINGS["api_key"], "".join(tool_content))
        after = result["events"][-1]["seq"]
        self.assertEqual(len(manager.snapshot(result["id"], after=after)["events"]), 1)

    def test_cancel_terminates_running_child_process(self):
        started = threading.Event()

        def execute(name, args, settings, context, cancel, progress):
            def report(line):
                started.set()
                progress(line)
            return web_app.run_local_process(
                [sys.executable, "-u", "-c", "import time; print('started', flush=True); time.sleep(30)"],
                timeout=40, cancel_event=cancel, on_progress=report,
            )

        manager, _, _ = self.manager([tool("repair", {})], execute)
        job = manager.start("修复歌词")
        self.assertTrue(started.wait(timeout=3))
        manager.cancel(job["id"])
        result = wait_job(manager, job)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(web_app.ACTIVE_PROCESSES)

    def test_cancel_during_model_request_does_not_wait_or_execute_tools(self):
        entered, release = threading.Event(), threading.Event()

        def chat(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return tool("repair", {})

        execute = Mock()
        manager = AgentManager(lambda: dict(SETTINGS), chat, execute)
        job = manager.start("修复歌词")
        self.assertTrue(entered.wait(timeout=2))
        try:
            started = time.monotonic()
            manager.cancel(job["id"])
            result = wait_job(manager, job)
            self.assertEqual(result["status"], "cancelled")
            self.assertLess(time.monotonic() - started, 1)
            execute.assert_not_called()
        finally:
            release.set()

    def test_malformed_api_response_fails_job(self):
        manager, _, execute = self.manager([{"content": None, "tool_calls": [{"function": {"name": []}}]}])
        result = wait_job(manager, manager.start("整理音乐"))
        self.assertEqual(result["status"], "failed")
        execute.assert_not_called()

    def test_frozen_tools_preserve_subdirectory(self):
        executable = "/tmp/Test.app/Contents/Helpers/WalkmanServer"
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", executable):
            command = web_app._python_script_command(web_app.PROJECT_ROOT / "tools/fill_lyrics.py", "/tmp/music")
        self.assertEqual(command, ["/tmp/Test.app/Contents/Helpers/WalkmanCLI", "tools/fill_lyrics.py", "/tmp/music"])

    def test_download_arguments_enable_album_and_translation_without_preview(self):
        command = web_app.action_command("download", {
            "track_ids": ["185868"], "output_dir": "/tmp/music", "by_album": True,
            "translate_japanese": True, "_translation_model": "test-model",
        }, apply=True)
        self.assertIn("--by-album", command)
        self.assertIn("--translate-japanese", command)
        self.assertIn("test-model", command)
        self.assertNotIn("--dry-run", command)

    def test_tools_reject_path_descriptions_and_missing_parameters(self):
        with self.assertRaises(ValueError):
            validate_arguments("convert", {"source": "   "})
        with self.assertRaises(ValueError):
            web_app.run_agent_tool("repair", {"root": "我的音乐库"}, SETTINGS,
                                   {"track_ids": set(), "bvids": set(), "user_text": ""}, threading.Event(), lambda _: None)

    def test_bilibili_postings_are_paginated_without_refetching(self):
        videos = [{"bvid": f"BVtest{i}", "title": f"翻唱 {i}", "created": 1000 - i} for i in range(250)]
        context = {"track_ids": set(), "bvids": set(), "user_text": "查询 UID 12345 的全部投稿"}

        def action(name, args, apply, **kwargs):
            Path(args["_result_path"]).write_text(json.dumps(videos))
            return {"ok": True, "output": "已查询"}

        with patch("web_app.run_action", side_effect=action) as executor:
            first = web_app.run_agent_tool("bili_list", {"uid": 12345}, SETTINGS, context, threading.Event(), lambda _: None)
            second = web_app.run_agent_tool("bili_list", {"uid": 12345, "offset": 100}, SETTINGS, context, threading.Event(), lambda _: None)
        executor.assert_called_once()
        self.assertEqual(first["data"]["next_offset"], 100)
        self.assertEqual(second["data"]["videos"][0]["bvid"], "BVtest100")
        self.assertIn("BVtest199", context["bvids"])
        self.assertNotIn("BVtest249", context["bvids"])

    def test_cover_and_lyrics_tools_accept_single_audio_file(self):
        tool_dir = web_app.PROJECT_ROOT / "tools"
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "path", [str(tool_dir), *sys.path]):
            audio = Path(directory) / "First Love.mp3"
            audio.write_bytes(b"test audio")
            for name in ("fill_lyrics", "embed_cover"):
                with self.subTest(name=name):
                    spec = importlib.util.spec_from_file_location(name, tool_dir / (name + ".py"))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    with patch.object(sys, "argv", [name, str(audio), "--dry-run"]):
                        if name == "embed_cover":
                            with patch.object(module, "has_cover", return_value=False):
                                self.assertEqual(module.main(), 0)
                        else:
                            self.assertEqual(module.main(), 0)
            self.assertEqual(audio.read_bytes(), b"test audio")
            self.assertFalse(audio.with_suffix(".lrc").exists())


class MusicAgentHTTPTests(unittest.TestCase):
    def test_http_api_runs_multi_step_job_and_serves_conversation_assets(self):
        captured = []
        scripted = [tool("search", {"query": "First Love"}), tool("download", {"track_ids": ["185868"]}),
                    {"content": "已下载 First Love。"}]

        class ModelHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"choices": [{"message": scripted[len(captured) - 1]}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
        model_thread.start()
        settings = {**SETTINGS, "api_base_url": f"http://127.0.0.1:{model_server.server_port}/v1"}

        def execute(name, args, config, context, cancel, progress):
            if name == "search":
                return {"ok": True, "data": {"tracks": [{"id": 185868, "name": "First Love"}]}, "output": "找到 1 首"}
            return {"ok": True, "output": "下载成功（测试工具，不写入文件）"}

        manager = AgentManager(lambda: settings, web_app.call_chat_message, execute)
        app_server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.WalkmanHandler)
        app_thread = threading.Thread(target=app_server.serve_forever, daemon=True)
        app_thread.start()
        base_url = f"http://127.0.0.1:{app_server.server_port}"
        try:
            with patch.object(web_app, "AGENT", manager), patch("web_app.load_settings", return_value=settings):
                request = Request(base_url + "/api/ai/run", data=json.dumps({"prompt": "找 First Love 并下载"}).encode(),
                                  headers={"Content-Type": "application/json"})
                with urlopen(request) as response:
                    self.assertEqual(response.status, 202)
                    job = json.loads(response.read())["job"]
                result = wait_job(manager, job)
                with urlopen(base_url + "/api/ai/jobs/" + job["id"] + "?after=1") as response:
                    progress = json.loads(response.read())["job"]
                self.assertEqual(result["status"], "completed")
                self.assertEqual(progress["result"], "已下载 First Love。")
                self.assertTrue(all(event["seq"] >= 1 for event in progress["events"]))
                self.assertIn("tools", captured[0])
                self.assertIn("tool_choice", captured[0])
                self.assertEqual(captured[1]["messages"][-1]["role"], "tool")
                self.assertEqual(json.loads(captured[1]["messages"][-1]["content"])["data"]["tracks"][0]["id"], 185868)
                with urlopen(base_url + "/agent.js") as response:
                    self.assertIn(b"/api/ai/run", response.read())
                with urlopen(base_url + "/api/settings") as response:
                    public = response.read()
                self.assertNotIn(settings["api_key"].encode(), public)
                blocked = Request(base_url + "/api/ai/run", data=b'{"prompt":"anything"}',
                                  headers={"Origin": "https://example.com", "Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(blocked)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
        finally:
            app_server.shutdown()
            app_server.server_close()
            model_server.shutdown()
            model_server.server_close()
            app_thread.join(timeout=2)
            model_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
