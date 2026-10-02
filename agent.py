"""Chat agent: Claude (via the Claude Agent SDK and the local Claude Code login) turns a plain-language
request into a decision-engine config, by investigating the data through a few in-process tools.

The agent has no built-in tools (no shell, no file access): only the tools defined here.
"""
import json
from datetime import datetime
from pathlib import Path

import yaml
from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock,
                              create_sdk_mcp_server, query, tool)

CONFIGS = Path(__file__).parent / "configs"  # every config the agent applies is saved here
MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are the analyst behind the "Homes England investment decision engine", a map of London \
that scores zones (H3 hexagons, MSOAs or boroughs) to support housing investment and site-selection discussions.

How the engine works:
- Each feature is converted to a percentile (0-1) across zones. Score = sum of weight x percentile over the \
enabled features (weights in [-1, 1]), then shown as a percentile among non-excluded zones.
- Hard exclusions remove a zone when an enabled layer covers at least its threshold share of the zone (0.05-1).
- "cluster" mode runs k-means on the percentiles of the enabled features instead (weights are ignored).

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


def text(obj):
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, indent=1)}]}


def make_server(describe, evaluate, on_apply):
    """Tools are built per request so apply_config can hand its config back to this request's stream."""

    @tool("describe_data", "Describe zone levels, hard-exclusion layers and features (with distributions and "
          "the most correlated feature pairs) at a given zone level.", {"zones": str})
    async def describe_data(args):
        return text(describe(args.get("zones") or "h3_8"))

    @tool("preview_config", "Evaluate a config (YAML string) without changing the user's map. Returns exclusion "
          "counts and the top/bottom zones (score mode) or cluster sizes and profiles (cluster mode).",
          {"config_yaml": str})
    async def preview_config(args):
        try:
            return text(evaluate(yaml.safe_load(args["config_yaml"]) or {}))
        except Exception as e:  # invalid YAML or config: let the agent fix it
            return {"content": [{"type": "text", "text": f"Invalid config: {e}"}], "is_error": True}

    @tool("apply_config", "Load the final config (YAML string) into the user's map. Call exactly once.",
          {"config_yaml": str})
    async def apply_config(args):
        try:
            cfg = yaml.safe_load(args["config_yaml"]) or {}
            evaluate(cfg)  # validates
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Invalid config: {e}"}], "is_error": True}
        CONFIGS.mkdir(exist_ok=True)
        path = CONFIGS / f"agent-{datetime.now():%Y%m%d-%H%M%S}.yaml"
        path.write_text(args["config_yaml"])
        on_apply(cfg, path.name)
        return text(f"Config loaded into the map (saved as configs/{path.name}).")

    tools = [describe_data, preview_config, apply_config]
    return create_sdk_mcp_server(name="engine", tools=tools), [f"mcp__engine__{t.name}" for t in tools]


async def chat(message, current_config, session_id, describe, evaluate):
    """Run one agent turn; yields events for the UI: text, tool, config, done/error."""
    applied = []
    server, tool_names = make_server(describe, evaluate, lambda cfg, name: applied.append((cfg, name)))
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"engine": server},
        tools=[],                      # no built-in tools (shell, files, web)
        allowed_tools=tool_names,
        permission_mode="dontAsk",     # anything else is denied without prompting
        setting_sources=[],            # ignore local Claude Code settings / CLAUDE.md
        resume=session_id,             # continue the same conversation across messages
        max_turns=25,
    )
    prompt = f"{message}\n\n<current_config>\n{current_config}\n</current_config>"
    try:
        async for msg in query(prompt=prompt, options=options):
            while applied:  # apply_config just ran: push the config to the map straight away
                cfg, name = applied.pop(0)
                yield {"type": "config", "config": cfg, "file": name}
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"type": "text", "text": block.text}
                    elif isinstance(block, ToolUseBlock):
                        yield {"type": "tool", "name": block.name.removeprefix("mcp__engine__")}
            elif isinstance(msg, ResultMessage):
                yield {"type": "done", "session_id": msg.session_id, "cost_usd": msg.total_cost_usd,
                       "error": msg.is_error}
    except Exception as e:
        yield {"type": "error", "text": str(e)}
