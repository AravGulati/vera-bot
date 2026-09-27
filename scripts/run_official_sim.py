"""Run magicpin's judge_simulator.py flows against the bot with a stub LLM (no API key needed).
Scoring falls back to the simulator's heuristic; the point is to exercise its exact HTTP calls."""
import importlib.util, os, sys
here = os.path.dirname(os.path.abspath(__file__))
sim_path = os.path.join(os.path.dirname(here), "judge_simulator.py")
spec = importlib.util.spec_from_file_location("judge_simulator", sim_path)
js = importlib.util.module_from_spec(spec); spec.loader.exec_module(js)
js.BOT_URL = os.getenv("BOT_URL", "http://localhost:8080")
class Stub(js.LLMProvider):
    def name(self): return "stub (no LLM)"
    def complete(self, prompt, system=None): return "ready"
import urllib.request
def teardown():
    urllib.request.urlopen(urllib.request.Request(js.BOT_URL + "/v1/teardown", data=b"{}", method="POST",
                                                  headers={"Content-Type": "application/json"}))
scenarios = sys.argv[1:] or ["all", "phase2_short", "full_evaluation"]
for sc in scenarios:
    teardown()  # fresh bot per scenario, as in the real harness
    judge = js.JudgeSimulator(Stub())
    judge.run(sc)
