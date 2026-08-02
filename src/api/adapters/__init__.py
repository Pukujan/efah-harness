"""Adapters for the application ports declared in :mod:`api.ports`.

Contract Section 5.1: all external systems go behind an adapter. The adapters
here are the ones the API itself owns and the composition root installs by
default; WS-B's TerminusDB adapter and WS-C's LangGraph adapter replace the
in-process ones by satisfying the same Protocols.
"""
