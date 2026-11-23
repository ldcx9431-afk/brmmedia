#!/usr/bin/env python3
"""Verify visual grounding and text/SSE compatibility; never print API keys."""
import argparse
import base64
import json
import re
import struct
import time
import urllib.request
import zlib


def image_data_url(ocr=False, scale=1):
    width, height = 400 * scale, 160 * scale
    pixels = bytearray([255] * (width * height * 3))

    def rectangle(x0, y0, x1, y1, rgb):
        x0, y0, x1, y1 = x0 * scale, y0 * scale, x1 * scale, y1 * scale
        for y in range(y0, y1):
            for x in range(x0, x1):
                pos = (y * width + x) * 3
                pixels[pos:pos + 3] = bytes(rgb)

    if ocr:
        glyphs = {
            "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
            "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
            "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
            "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
            "2": ["11110", "00001", "00001", "01110", "10000", "10000", "11111"],
        }
        for index, character in enumerate("BRM42"):
            for y, row in enumerate(glyphs[character]):
                for x, bit in enumerate(row):
                    if bit == "1":
                        x0, y0 = 25 + index * 70 + x * 10, 40 + y * 10
                        rectangle(x0, y0, x0 + 10, y0 + 10, (0, 0, 0))
    else:
        rectangle(30, 30, 130, 130, (255, 0, 0))
        rectangle(270, 30, 370, 130, (0, 0, 255))

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)

    rows = b"".join(b"\0" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


def request(url, payload=None, key=None, method=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=180)


def verify(base, model, key=None):
    def completion(content, stream=False):
        return request(base + "/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": content}],
            "temperature": 0, "max_tokens": 128, "stream": stream,
            "chat_template_kwargs": {"enable_thinking": False},
        }, key)

    with request(base + "/models", key=key) as response:
        assert model in [row["id"] for row in json.load(response)["data"]]
    with completion("只回复：TEXT_OK") as response:
        answer = json.load(response)["choices"][0]["message"]["content"]
        assert "TEXT_OK" in answer, answer
    print("[PASS] Text completion", flush=True)
    for ocr, scale, prompt in [(False, 1, "图片左侧和右侧的方块分别是什么颜色？请简短回答。"), (True, 1, "读取图片中的大写英文字母和数字，只返回识别的文字。"), (False, 4, "图片左侧和右侧的方块分别是什么颜色？请简短回答。")]:
        started = time.monotonic()
        with completion([{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": image_data_url(ocr, scale)}}]) as response:
            result = json.load(response)
        answer = result["choices"][0]["message"]["content"]
        if ocr:
            assert "BRM42" in re.sub(r"[^A-Z0-9]", "", answer.upper()), answer
        else:
            assert ("红" in answer or "red" in answer.lower()) and ("蓝" in answer or "blue" in answer.lower()), answer
        print(json.dumps({"test": "ocr" if ocr else "image_colors", "dimensions": [400 * scale, 160 * scale], "answer": answer, "seconds": round(time.monotonic() - started, 2), "usage": result.get("usage")}), flush=True)
    for content, expected in [
        ("只回复：STREAM_OK", "STREAM_OK"),
        ([{"type": "text", "text": "读取图片中的大写英文字母和数字，只返回识别的文字。"},
          {"type": "image_url", "image_url": {"url": image_data_url(True)}}], "BRM42"),
    ]:
        with completion(content, stream=True) as response:
            answer = ""
            done = False
            for line in response:
                if not line.startswith(b"data: "):
                    continue
                payload = line[6:].strip()
                if payload == b"[DONE]":
                    done = True
                    break
                choices = json.loads(payload).get("choices") or []
                if choices:
                    answer += choices[0].get("delta", {}).get("content") or ""
            assert expected in re.sub(r"\s", "", answer) and done, answer
        print("[PASS] " + ("Image" if isinstance(content, list) else "Text") + " SSE completion", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8001/v1")
    parser.add_argument("--model", default="qwen38-27b-ud-q4-k-m")
    parser.add_argument("--gateway", action="store_true", help="Also verify Nginx/Bearer with a temporary one-day key, then delete it")
    args = parser.parse_args()
    verify(args.base.rstrip("/"), args.model)
    if args.gateway:
        admin = "http://127.0.0.1:9300/admin/api-keys"
        with request(admin, {"name": "Qwen vision deployment acceptance", "expires_in_days": 1}) as response:
            created = json.load(response)
        try:
            verify("http://127.0.0.1/qwen-api/v1", args.model, created["api_key"])
        finally:
            with request(admin + "/" + created["id"] + "/disable", {}, method="POST"):
                pass
            with request(admin + "/" + created["id"], method="DELETE"):
                pass
            print("[OK] Temporary acceptance key physically deleted", flush=True)
    print("[PASS] Qwen vision acceptance", flush=True)


if __name__ == "__main__":
    main()
