import unittest

import httpx

from ai_video.video_higgsfield_jutsu.python_backend.agent import request_openai
from ai_video.video_higgsfield_jutsu.python_backend.ai_usage import usage_row
from ai_video.video_higgsfield_jutsu.python_backend.store import JobStore
from ai_video.video_higgsfield_jutsu.python_backend.wan import WanClient


class UsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_records_each_response_and_keeps_zero_cached_tokens(self):
        payload = {"usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                             "input_tokens_details": {"cached_tokens": 0}}}
        rows = []
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json=payload, headers={"x-request-id": "req_123"},
        ))
        async with httpx.AsyncClient(transport=transport) as http:
            response = await request_openai(
                http, "test-key", "responses", {"model": "test-model"}, lambda *args: None,
                usage_sink=rows.append, job_id="job-1", session_id="session-1",
            )
        self.assertEqual(response, payload)
        self.assertEqual(rows[0]["cached_tokens"], 0)
        self.assertEqual(rows[0]["total_tokens"], 14)
        self.assertEqual(rows[0]["provider_request_id"], "req_123")
        self.assertEqual(rows[0]["session_id"], "session-1")
        self.assertNotIn("cost_usd", rows[0])

        store = JobStore()
        job, _, _ = store.create("Test scene", None, 5, "16:9")
        store.emit(job["id"], "ai_usage", rows[0])
        event = store.get(job["id"])["events"][-1]
        self.assertEqual(event["value"]["input_tokens"], 10)
        self.assertEqual(event["value"]["raw_usage"]["input_tokens_details"]["cached_tokens"], 0)

    async def test_openai_usage_sink_failure_does_not_fail_model_call(self):
        def unavailable(_):
            raise RuntimeError("usage storage unavailable")

        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"usage": []}))
        async with httpx.AsyncClient(transport=transport) as http:
            response = await request_openai(
                http, "test-key", "responses", {"model": "test-model"}, lambda *args: None,
                usage_sink=unavailable,
            )
        self.assertEqual(response, {"usage": []})

    async def test_wan_keeps_top_level_usage_with_both_durations(self):
        payload = {"request_id": "provider-request",
                   "output": {"task_status": "SUCCEEDED", "video_url": "https://example.com/video.mp4"},
                   "usage": {"duration": 15.0, "output_video_duration": 10.0, "video_count": 1}}
        client = WanClient("test-key", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload),
        ))
        result = await client.poll("task-1", "singapore")
        self.assertEqual(result["video_url"], payload["output"]["video_url"])
        self.assertEqual(result["_usage"], payload["usage"])
        row = usage_row("dashscope", client.capabilities.model, {"usage": result["_usage"]},
                        provider_request_id=result["_request_id"], status="succeeded")
        self.assertEqual(row["billing_unit"], "second")
        self.assertEqual(row["quantity"], 15)
        self.assertEqual(row["duration_seconds"], 10)
        self.assertNotIn("total_tokens", row)
