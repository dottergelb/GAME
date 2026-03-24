# openai_vision_table.py
import base64
import json
import os
from typing import List

from openai import OpenAI
from pydantic import BaseModel, Field

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class TableParse(BaseModel):
    players: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


SYSTEM_PROMPT = "Extract final results table from screenshot. Return ONLY valid JSON."

USER_PROMPT = """
Return ONLY JSON:
{
  "players": ["nick1","nick2","nick3","nick4","nick5","nick6","nick7","nick8"],
  "notes": []
}
Rules:
- EXACT nicknames as shown (case/spaces)
- Order top -> bottom
- If you see less than 8, return what you see and explain in notes
- No extra text outside JSON
"""


def to_data_url(image_bytes: bytes) -> str:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def extract_player_names(image_bytes: bytes) -> TableParse:
    data_url = to_data_url(image_bytes)

    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": USER_PROMPT},
                    {"type": "input_image", "image_url": data_url},
                ],
            },
        ],
    )

    raw = (resp.output_text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"Model did not return JSON. Raw: {raw[:200]}")

    data = json.loads(raw[start:end + 1])
    return TableParse.model_validate(data)
