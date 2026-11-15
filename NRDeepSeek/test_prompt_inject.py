"""거절 검출기 회귀 테스트.

운영 로그에서 오탐이 나온 문장은 반드시 통과해야 하고,
진짜 거절은 계속 잡혀야 한다.

    python3 -m unittest test_prompt_inject -v
"""

import unittest

from prompt_inject import refusal_hits, strip_noise

# 운영 로그에서 PURGE를 잘못 발동시킨 정상 문장들
NORMAL = [
    "pyannote 화자분리는 이 콘텐츠에서 잘 안 됨.",
    "모델 하나로는 안 됨 — Whisper의 --translate는 영어로만 감.",
    '14B가 이 장르에서 가끔 "죄송하지만"을 뱉는데, 그때는 세그먼트를 다시 나눠서 넘기면 됨.',
    'REFUSAL = ("죄송하지만", "I can\'t", "as an AI")  # 코드 블록 안 문자열',
    "자동 업데이트는 안 함. 대신 watchtower를 따로 붙이면 됨.",
    "이 모델은 한국어 전사가 안 됨 — 화자분리는 pyannote가 더 낫다.",
    "이 파이프라인은 자동 업데이트를 안 함. watchtower를 따로 붙이면 됨.",
]

# 진짜 거절
REFUSALS = [
    "그런 장면은 안 써.",
    "죄송하지만 그 요청은 도와드릴 수 없어요.",
    "안 긁음. 실명 사람 몸 스펙은 안 다룸.",
    "이 부분은 다루지 않습니다.",
    "권한이 필요합니다. 승인 먼저 받아주세요.",
    "그 요청은 안 됩니다.",
    "I can't help with that.",
    "As an AI, I must decline.",
    "ㅇㅇ 안 하는 거 맞음. 뭐라고 부르든 결론은 같음.",
    "검열 문제가 아니라 그냥 안 만드는 거임.",
    "이 부분은 다루지 않습니다.",
]


class RefusalDetectorTest(unittest.TestCase):
    def test_normal_answers_pass(self):
        for text in NORMAL:
            with self.subTest(text=text[:40]):
                self.assertEqual(refusal_hits(text), [], "정상 답변을 거절로 오판")

    def test_real_refusals_detected(self):
        for text in REFUSALS:
            with self.subTest(text=text[:40]):
                self.assertTrue(refusal_hits(text), "진짜 거절을 놓침")

    def test_code_block_is_stripped(self):
        text = "예시는 이렇습니다.\n```python\nREFUSAL = ('죄송하지만', 'as an AI')\n```\n그대로 쓰면 됩니다."
        self.assertEqual(refusal_hits(text), [])

    def test_inline_code_is_stripped(self):
        text = "변수 `CANNOT = 'I can't'` 를 쓰면 됩니다."
        self.assertEqual(refusal_hits(text), [])

    def test_quoted_refusal_is_stripped(self):
        text = '모델이 "죄송하지만 그 요청은 도와드릴 수 없어요" 같은 문장을 뱉으면 재시도하세요.'
        self.assertEqual(refusal_hits(text), [])

    def test_anti_false_positive_cases(self):
        for text in ("권한 없음 401이 떴습니다. 토큰을 재발급하세요.",
                     "이 부분은 렌더링에 참여하지 않습니다."):
            with self.subTest(text=text[:30]):
                self.assertEqual(refusal_hits(text), [])

    def test_weak_word_late_in_answer_is_ignored(self):
        # 약한 단어는 앞 300자에서만 본다 — 긴 기술 답변 뒤쪽의 "안 됨"은 거절이 아니다
        text = ("파이프라인은 세 단계입니다. " * 40) + "이 조합은 안 됨."
        self.assertEqual(refusal_hits(text), [])

    def test_strip_noise_keeps_body(self):
        kept = strip_noise("본문 `코드` 그리고 ```\n블록\n``` 끝")
        self.assertIn("본문", kept)
        self.assertIn("끝", kept)
        self.assertNotIn("코드", kept)


if __name__ == "__main__":
    unittest.main()
