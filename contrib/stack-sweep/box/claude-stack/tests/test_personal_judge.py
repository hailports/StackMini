import asyncio, sys
sys.path.insert(0, "/home/operator/claude-stack")
from core import free_judge as fj

def test_makers():
    assert fj.maker_of(("groq:gpt-oss-120b", "groq", "openai", "openai/gpt-oss-120b")) == "openai"
    assert fj.maker_of(("or:grok", "openrouter", "openrouter", "x-ai/grok-5")) == "xai"
    assert fj.maker_of(fj.PERSONAL[0]) == "openai" and fj.maker_of(fj.PERSONAL[1]) == "anthropic"
    assert fj.personal_primary("x_hailports_loop", '{"score": 0-10}', None)
    assert fj.personal_primary("linkedin_judge", '{"score": 0-10}', None)
    assert not fj.personal_primary("x_ima", '{"score": 0-10}', None)

def test_pair_rules():
    async def fake_cli(j, p, s, l):
        return '{"why":"x","score":9}' if j[2] == "cli_codex" else ""   # claude expired
    async def fake_judge(j, p, s, m, l):
        return '{"why":"y","score":9}'
    fj._cli_judge, fj._judge = fake_cli, fake_judge
    fj._healthy = lambda lane, **k: [("groq:gpt-oss-120b", "groq", "openai", "openai/gpt-oss-120b"),
                                     ("gemini-3.5-flash", "google", "gemini", "gemini-3.5-flash")]
    t, p = asyncio.run(fj._personal_core('{"score"}', None, 160, "x_hailports"))
    assert fj.score_of(t) == 9 and "gemini" in p and "gpt-oss" not in p, p
    fj._healthy = lambda lane, **k: [("groq:gpt-oss-120b", "groq", "openai", "openai/gpt-oss-120b")]
    async def no_paid(*a, **k): return None
    fj._paid_overflow = no_paid
    t, p = asyncio.run(fj._personal_core('{"score"}', None, 160, "x_hailports"))
    assert fj.score_of(t) == 8 and "capped" in p, p   # same maker never pairs -> capped (fail closed on a 9)

if __name__ == "__main__":
    test_makers(); test_pair_rules(); print("PASS")
