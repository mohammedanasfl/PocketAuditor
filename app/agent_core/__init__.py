"""The tool-using agent (docs/AGENT_DESIGN.md §5).

- tools.py     the tools the model can call (plain async functions)
- policy.py    checks every write before it runs: allow / ask / deny
- loop.py      the agent loop: model → tool calls → results → repeat
- prompts.py   system prompts
- reconcile.py processes pending transactions with the agent
- chat.py      answers free-form chat messages with the agent
"""
