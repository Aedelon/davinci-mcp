# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Recipes: multi-step workflows run server-side in one MCP call.

YAML recipes are declarative chains of existing actions (safe by construction: every
step goes through the normal pipeline, its validation, receipts and audit). Python
recipes are arbitrary code and are only loaded when ``recipes.allow_python`` is on.
"""
