"""Route modules; each exposes ``router``."""

from . import (
    ai,
    appauth,
    appusers,
    auth,
    csvdata,
    delivery,
    governance,
    inbound,
    integrations,
    jobs,
    logs,
    mcp,
    mcpservers,
    oidc,
    orgs,
    pages,
    preview,
    processes,
    projects,
    proposals,
    tasks,
    testruns,
    tokens,
    tools,
    webhooks,
)

ALL = (auth, orgs, projects, proposals, ai, preview, testruns, tokens, tools, mcp, jobs, delivery, governance, appusers, appauth, webhooks, csvdata, inbound,
       integrations, mcpservers, processes, tasks, pages, oidc, logs)
