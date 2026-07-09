# Novelpilot Documentation

This directory contains the smallest public documentation set needed to understand Novelpilot
without exposing local Trellis or agent-workspace files.

Novelpilot is a local, single-user writing workbench for long-form AI novel generation. The main
engineering idea is a three-layer Agent Loop Harness: the LLM performs semantic work, while the
harness controls context, artifacts, verification, routing, and committed state.

## Reading Order

1. [Architecture](./architecture.md)
   Explains the product goal, the three loop layers, candidate-versus-committed boundaries, storage,
   run control, and completion evidence.

2. [Local Usage](./local-usage.md)
   Explains how to run the app locally, configure LLM profiles, create a novel project, start the
   harness, export a manuscript, and run validation.

The root [README](../README.md) remains the quick-start entry point. These docs are the compact
public replacement for the deeper planning notes that are kept in local-only Trellis files.

