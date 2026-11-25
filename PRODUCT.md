# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

React and TypeScript for the board. The local Python service serves the built assets and APIs.

## Users

Operators monitoring coding sessions and jobs sharing one workstation. The board supports reading resource contention and communicating with sessions from the same local interface.

## Product Purpose

Arbiter schedules shared resources and keeps coordination visible. The board shows who is running, what is waiting, and which decisions need operator attention.

## Operating Context

The board presents live session updates, granted and waiting reservations, machine capacity, approval requests, messages, operator decisions, and available conversation transcripts. Operator controls use the board’s existing authenticated, same-origin endpoints.

## Capabilities and Constraints

- The board uses live updates and periodically refreshes machine readings.
- Operator sign-in uses the workstation account or a single-use launch link from the CLI.
- The board uses a single dark Observatory palette. Session grouping and board area can be changed independently.
- Operators can combine keyword search and quick filters with any session grouping. Queued resource reservations and pending approvals are independent filters.
