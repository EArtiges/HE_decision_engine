"""Chat agent: a Microsoft Foundry prompt agent turns a plain-language request into a decision-engine config,
by investigating the data through a few function tools that run here, in the app, and searching the programme
documents with Foundry file search (vector store kept up to date by refresh/docs_sync.py).

The agent is registered in the Foundry project at first use (a new version only when the instructions or tools
change). Its only Foundry-hosted tool is file search: no web search or code interpreter.
Auth is Entra ID via DefaultAzureCredential (`az login` locally, managed identity when deployed).
"""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

import yaml
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import FileSearchTool, FunctionTool, PromptAgentDefinition
from azure.identity.aio import DefaultAzureCredential

CONFIGS = Path(__file__).parent / "configs"  # every config the agent applies is saved here
ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT",
                          "https://ai-applications-uk-resource.services.ai.azure.com/api/projects/ai-applications-uk")
MODEL = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT", "gpt-6-luna")
AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "he-decision-engine")
# file search citation markers the model writes inline: private-use characters around words like filecite, turn0file1
CITATION = re.compile("[\ue000-\uf8ff][\\w\ue000-\uf8ff]*")
VECTOR_STORE = "he-programme-documents"  # created and refreshed by refresh/docs_sync.py
MAX_TURNS = 25  # model round-trips per user message

SYSTEM_PROMPT = """You are the analyst behind the "Homes England investment decision engine", a map of London \
that scores zones (H3 hexagons, MSOAs or boroughs) to support housing investment and site-selection discussions.

How the engine works:
- Each feature is converted to a percentile (0-1) across zones. Score = sum of weight x percentile over the \
enabled features (weights in [-1, 1]), then shown as a percentile among non-excluded zones.
- Hard exclusions remove a zone when an enabled layer covers at least its threshold share of the zone (0.05-1).
- "cluster" mode runs k-means on the percentiles of the enabled features instead (weights are ignored).

You also have Homes England programme documents (funding guidance and prospectuses), searchable with \
file_search (each document starts with its title, source URL and last updated date). When the user mentions a programme, fund or policy (e.g. "candidates for \
the BIL Fund"), ground your answer in it: search for its eligibility, prioritisation and assessment criteria \
(several targeted searches are fine), then map each criterion to the engine's features and exclusions. Say \
explicitly which criteria the data can't represent (e.g. viability, applicant type, economic growth potential) \
rather than inventing proxies, and cite the sections you relied on by name.

The user describes what they want to see. Your job:
1. Call describe_data to learn the features, exclusions and their distributions (once is usually enough).
2. Draft a config and call preview_config to check what it produces (top zones, exclusions, cluster profiles). \
Iterate if the result doesn't match the intent: e.g. too many zones excluded, a feature dominating, \
redundant highly correlated features, wrong sign on a distance feature (higher distance = further away).
3. Call apply_config exactly once with the final config: this loads it into the user's map.
4. Reply in a few short sentences: what you optimised for, the key weights and exclusions and why, and \
anything the user should know (data caveats, trade-offs). Plain text only: no Markdown (no **bold**, headings, \
bullet lists or tables), as the chat window shows raw text.

Config format (YAML, same as the app's export):
zones: h3_8 | h3_7 | msoa | boroughs
mode: score | cluster
n_clusters: 1-10
hard_exclusions:
  <layer key>: {enabled: true|false, threshold: 0.05-1}
features:
  <feature key>: {enabled: true|false, weight: -1..1}
Features or exclusions you leave out are switched off. Prefer a handful of meaningful features over many. \
Keep the user's current zones and mode unless their request implies a change. If the request is ambiguous, \
make a sensible choice and say what you assumed rather than asking."""


def config_arg(description):
    return {"type": "object", "properties": {"config_yaml": {"type": "string", "description": description}},
            "required": ["config_yaml"]}


TOOLS = [
    FunctionTool(name="describe_data", strict=False,
                 description="Describe zone levels, hard-exclusion layers and features (with distributions and "
                             "the most correlated feature pairs) at a given zone level.",
                 parameters={"type": "object", "properties": {"zones": {
                     "type": "string", "description": "Zone level: h3_8, h3_7, msoa or boroughs (default h3_8)"}}}),
    FunctionTool(name="preview_config", strict=False,
                 description="Evaluate a config (YAML string) without changing the user's map. Returns exclusion "
                             "counts and the top/bottom zones (score mode) or cluster sizes and profiles (cluster mode).",
                 parameters=config_arg("Config in the app's YAML export format")),
    FunctionTool(name="apply_config", strict=False,
                 description="Load the final config (YAML string) into the user's map. Call exactly once.",
                 parameters=config_arg("Final config in the app's YAML export format")),
]


def text(obj):
    return obj if isinstance(obj, str) else json.dumps(obj, indent=1)


def run_tool(name, args, describe, evaluate, on_apply):
    """Runs one function call from the agent; errors are returned as text so the agent can fix its input."""
    if name == "describe_data":
        return text(describe(args.get("zones") or "h3_8"))
    if name in ("preview_config", "apply_config"):
        try:
            cfg = yaml.safe_load(args["config_yaml"]) or {}
            result = evaluate(cfg)  # validates
        except Exception as e:  # invalid YAML or config: let the agent fix it
            return f"Invalid config: {e}"
        if name == "preview_config":
            return text(result)
        CONFIGS.mkdir(exist_ok=True)
        path = CONFIGS / f"agent-{datetime.now():%Y%m%d-%H%M%S}.yaml"
        path.write_text(args["config_yaml"])
        on_apply(cfg, path.name)
        return f"Config loaded into the map (saved as configs/{path.name})."
    return f"Unknown tool {name!r}"


_openai = None  # AsyncOpenAI client for the project, created (and the agent registered) on first chat


async def client():
    global _openai
    if _openai is None:
        project = AIProjectClient(endpoint=ENDPOINT, credential=DefaultAzureCredential())
        openai = project.get_openai_client()
        stores = [vs.id async for vs in openai.vector_stores.list() if vs.name == VECTOR_STORE]
        if not stores:
            raise RuntimeError(f"No vector store {VECTOR_STORE!r} in the Foundry project: run uv run refresh/docs_sync.py")
        definition = PromptAgentDefinition(model=MODEL, instructions=SYSTEM_PROMPT,
                                           tools=TOOLS + [FileSearchTool(vector_store_ids=stores[:1])])
        digest = hashlib.sha256(json.dumps(definition.as_dict(), sort_keys=True).encode()).hexdigest()[:16]
        try:
            latest = (await project.agents.get(AGENT_NAME)).versions.latest
            current = (latest.metadata or {}).get("definition_hash") == digest
        except Exception:  # agent doesn't exist yet
            current = False
        if not current:  # new agent, or this code's instructions/tools differ from the latest version
            await project.agents.create_version(AGENT_NAME, definition=definition,
                                                metadata={"definition_hash": digest},
                                                description="Homes England investment decision engine analyst")
        _openai = openai
    return _openai


async def chat(message, current_config, session_id, describe, evaluate):
    """Run one agent turn; yields events for the UI: text, tool, config, done/error.

    session_id is the Foundry conversation id: follow-up messages continue the same conversation."""
    applied = []
    try:
        openai = await client()
        conversation = session_id or (await openai.conversations.create()).id
        items = [{"type": "message", "role": "user",
                  "content": f"{message}\n\n<current_config>\n{current_config}\n</current_config>"}]
        tokens = 0
        for _ in range(MAX_TURNS):
            response = await openai.responses.create(
                conversation=conversation, input=items,
                extra_body={"agent_reference": {"name": AGENT_NAME, "type": "agent_reference"}})
            tokens += response.usage.total_tokens if response.usage else 0
            items = []  # outputs of this round's function calls, sent back in the next request
            for out in response.output:
                if out.type == "message":
                    reply = CITATION.sub("", "".join(c.text for c in out.content if c.type == "output_text"))
                    if reply.strip():
                        yield {"type": "text", "text": reply}
                elif out.type == "file_search_call":  # runs in Foundry: only shown in the UI
                    yield {"type": "tool", "name": "file_search"}
                elif out.type == "function_call":
                    yield {"type": "tool", "name": out.name}
                    result = run_tool(out.name, json.loads(out.arguments or "{}"), describe, evaluate,
                                      lambda cfg, name: applied.append((cfg, name)))
                    while applied:  # apply_config just ran: push the config to the map straight away
                        cfg, name = applied.pop(0)
                        yield {"type": "config", "config": cfg, "file": name}
                    items.append({"type": "function_call_output", "call_id": out.call_id, "output": result})
            if not items:  # no function calls: the agent has answered
                yield {"type": "done", "session_id": conversation, "tokens": tokens, "error": False}
                return
        yield {"type": "done", "session_id": conversation, "tokens": tokens, "error": True}  # hit MAX_TURNS
    except Exception as e:
        yield {"type": "error", "text": str(e)}
