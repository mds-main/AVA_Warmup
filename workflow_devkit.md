# Workflow DevKit Documentation

## Getting Started - Next.js

### Create Project & Install

```bash
npm create next-app@latest my-workflow-app
cd my-workflow-app
npm i workflow
```

### Configure Next.js

```typescript title="next.config.ts"
import { withWorkflow } from 'workflow/next';
import type { NextConfig } from 'next';

const nextConfig: NextConfig = { /* config */ };
export default withWorkflow(nextConfig);
```

**Optional TypeScript IntelliSense:**
```json title="tsconfig.json"
{
  "compilerOptions": {
    "plugins": [{ "name": "workflow" }]
  }
}
```

**Optional Proxy Handler Configuration:**
```typescript title="proxy.ts"
export const config = {
  matcher: [{
    source: '/((?!_next/static|_next/image|favicon.ico|.well-known/workflow/).*)',
  }],
};
```

### Create Workflow & Steps

```typescript title="workflows/user-signup.ts"
import { sleep, FatalError } from "workflow";

export async function handleUserSignup(email: string) {
  "use workflow";
  
  const user = await createUser(email);
  await sendWelcomeEmail(user);
  await sleep("5s"); // Suspend without resources
  await sendOnboardingEmail(user);
  
  return { userId: user.id, status: "onboarded" };
}

async function createUser(email: string) {
  "use step";
  console.log(`Creating user: ${email}`);
  return { id: crypto.randomUUID(), email };
}

async function sendWelcomeEmail(user: { id: string; email: string }) {
  "use step";
  console.log(`Sending welcome email to: ${user.id}`);
  
  if (Math.random() < 0.3) {
    throw new Error("Retryable!"); // Auto-retry on error
  }
}

async function sendOnboardingEmail(user: { id: string; email: string }) {
  "use step";
  
  if (!user.email.includes("@")) {
    throw new FatalError("Invalid Email"); // Skip retry
  }
  
  console.log(`Sending onboarding email to: ${user.id}`);
}
```

### Create Route Handler

```typescript title="app/api/signup/route.ts"
import { start } from 'workflow/api';
import { handleUserSignup } from "@/workflows/user-signup";
import { NextResponse } from "next/server";

export async function POST(request: Request) {
  const { email } = await request.json();
  await start(handleUserSignup, [email]); // Async execution
  return NextResponse.json({ message: "Workflow started" });
}
```

### Run Development

```bash
npm run dev
curl -X POST --json '{"email":"hello@example.com"}' http://localhost:3000/api/signup
```

**Inspect workflows:**
```bash
npx workflow inspect runs
npx workflow inspect runs --web  # Interactive UI
```

### Deploy to Production

Works best on [Vercel](https://vercel.com) with no special configuration. See [Deploying](/docs/deploying) for other platforms.

---

## Foundations

### Workflows and Steps

**Workflow functions** (`"use workflow"`) orchestrate steps deterministically without Node.js access. They suspend/resume across failures and maintain state via event logs.

**Step functions** (`"use step"`) perform actual work with full Node.js access, automatic retry on errors, and persisted results.

```typescript
// Workflow: orchestrates, deterministic, sandboxed
export async function processOrderWorkflow(orderId: string) {
  "use workflow";
  const order = await fetchOrder(orderId);
  const payment = await chargePayment(order);
  return { orderId, status: 'completed' };
}

// Step: full runtime access, retryable
async function fetchOrder(orderId: string) {
  "use step";
  return await db.query('SELECT * FROM orders WHERE id = ?', [orderId]);
}
```

**Suspension mechanisms:**
- Waiting on step functions
- `sleep()` for fixed durations
- `createWebhook()` for external data

**Key insight:** Workflows resume by replaying code using cached step results. Deterministic execution (seeded `Math.random()`, `Date`) ensures consistent replay behavior.

**Project structure:**
```
workflows/
  userOnboarding/
    index.ts
    steps.ts
  aiVideoGeneration/
    index.ts
    steps/
      transcribeUpload.ts
      generateVideo.ts
  shared/
    validateInput.ts
```

### Starting Workflows

```typescript
import { start } from 'workflow/api';
import { processOrder } from './workflows/process-order';

// Fire and forget
const run = await start(processOrder, [orderId]);
console.log('Run ID:', run.runId);

// Wait for completion
const result = await run.returnValue;

// Check status later
const status = await run.status; // 'running' | 'completed' | 'failed'

// Stream updates
const stream = run.getReadable();
return new Response(stream);

// Retrieve existing run
import { getRun } from 'workflow/api';
const run = getRun(runId);
```

**Run object properties:**
- `runId` - Unique identifier
- `status` - Current status (async)
- `returnValue` - Workflow result (async, blocks until complete)
- `readable` - ReadableStream for updates

### Control Flow Patterns

**Sequential:**
```typescript
const validated = await validateData(data);
const processed = await processData(validated);
const stored = await storeData(processed);
```

**Parallel:**
```typescript
const [user, orders, preferences] = await Promise.all([
  fetchUser(userId),
  fetchOrders(userId),
  fetchPreferences(userId)
]);
```

**Race with timeout:**
```typescript
const webhook = createWebhook();
await executeExternalTask(webhook.url);

await Promise.race([
  webhook,
  sleep('1 day')
]);
```

### Errors & Retrying

**Default behavior:** Steps retry up to 3 times on errors. Customize with `maxRetries`:

```typescript
async function callApi(endpoint: string) {
  "use step";
  const response = await fetch(endpoint);
  
  if (response.status >= 500) {
    throw new Error("Retryable"); // Auto-retry
  }
  
  if (response.status === 404) {
    throw new FatalError("Not found"); // Skip retry
  }
  
  if (response.status === 429) {
    const retryAfter = response.headers.get("Retry-After");
    throw new RetryableError("Too many requests", {
      retryAfter: parseInt(retryAfter) // Custom delay
    });
  }
  
  return response.json();
}
callApi.maxRetries = 5;
```

**Exponential backoff:**
```typescript
import { getStepMetadata } from "workflow";

async function callApi(endpoint: string) {
  "use step";
  const metadata = getStepMetadata();
  
  if (response.status >= 500) {
    throw new RetryableError("Backing off", {
      retryAfter: metadata.attempt ** 2 // Exponential
    });
  }
}
```

**Rollback pattern:**
```typescript
export async function placeOrderSaga(orderId: string) {
  "use workflow";
  const rollbacks: Array<() => Promise<void>> = [];
  
  try {
    await reserveInventory(orderId);
    rollbacks.push(() => releaseInventory(orderId));
    
    await chargePayment(orderId);
    rollbacks.push(() => refundPayment(orderId));
  } catch (e) {
    for (const rollback of rollbacks.reverse()) {
      await rollback();
    }
    throw e;
  }
}
```

### Hooks & Webhooks

**Hooks** are low-level primitives for pausing workflows and resuming with arbitrary data:

```typescript
import { createHook, defineHook } from "workflow";

// Basic usage
export async function approvalWorkflow() {
  "use workflow";
  const hook = createHook<{ approved: boolean; comment: string }>();
  
  console.log("Token:", hook.token);
  const result = await hook;
  
  if (result.approved) {
    console.log("Approved:", result.comment);
  }
}

// Resume from external code
import { resumeHook } from "workflow/api";
await resumeHook(token, { approved, comment });

// Custom deterministic tokens
const hook = createHook<SlackMessage>({
  token: `slack_messages:${channelId}`
});

// Multiple events with iteration
for await (const payload of hook) {
  console.log(payload);
  if (payload.done) break;
}

// Type-safe hooks
const approvalHook = defineHook<ApprovalRequest>();
const hook = approvalHook.create({ token: `approval:${documentId}` });
await approvalHook.resume(`approval:${documentId}`, approvalData);
```

**Webhooks** build on hooks, serializing entire HTTP `Request` objects with automatic URL routing:

```typescript
import { createWebhook } from "workflow";

export async function webhookWorkflow() {
  "use workflow";
  const webhook = createWebhook();
  
  console.log("URL:", webhook.url);
  // Auto-available at: /.well-known/workflow/v1/webhook/:token
  
  const request = await webhook;
  const data = await request.json(); // Auto-cached as step
}

// Static response
const webhook = createWebhook({
  respondWith: new Response(
    JSON.stringify({ success: true }),
    { status: 200, headers: { "Content-Type": "application/json" } }
  )
});

// Dynamic response (requires step)
async function sendCustomResponse(request: RequestWithResponse, msg: string) {
  "use step";
  await request.respondWith(
    new Response(JSON.stringify({ message: msg }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    })
  );
}

export async function webhookWithDynamicResponse() {
  "use workflow";
  const webhook = createWebhook({ respondWith: "manual" });
  const request = await webhook;
  const data = await request.json();
  
  await sendCustomResponse(request, data.type === "urgent" ? "Urgent" : "Normal");
}

// Multiple webhook events
for await (const request of webhook) {
  const formData = await request.formData();
  const command = formData.get("command");
  
  if (command === "/stop") break;
  await processCommand(command);
}
```

**Hooks vs Webhooks:**
| Feature | Hooks | Webhooks |
|---------|-------|----------|
| Data Format | Arbitrary serializable | HTTP `Request` |
| URL | Manual | Automatic `webhook.url` |
| Response | N/A | Static or dynamic `Response` |
| Resume | `resumeHook()` | Automatic via HTTP or `resumeWebhook()` |

### Serialization

All workflow arguments and return values must be serializable. Built on [devalue](https://github.com/sveltejs/devalue).

**Supported types:**
- **JSON:** `string`, `number`, `boolean`, `null`, arrays, objects
- **Extended:** `undefined`, `bigint`, `ArrayBuffer`, typed arrays, `Date`, `Map`, `Set`, `RegExp`, `URL`, `URLSearchParams`, `Headers`, `Request`, `Response`, `ReadableStream`, `WritableStream`

**Streaming:**
Streams are serializable but **cannot be used directly in workflows**—only passed between steps:

```typescript
// ✅ Correct: Stream passed between steps
async function generateStream() {
  "use step";
  return new ReadableStream({ /* ... */ });
}

async function consumeStream(readable: ReadableStream<number>) {
  "use step";
  for await (const value of readable) {
    console.log(value);
  }
}

export async function passingStreamWorkflow() {
  "use workflow";
  const readable = await generateStream();
  await consumeStream(readable); // Pass through, don't use
}

// ❌ Incorrect: Direct stream usage in workflow
export async function incorrectStreamUsage() {
  "use workflow";
  const readable = await generateStream();
  const reader = readable.getReader(); // Error!
}
```

**Request & Response convenience:**
Calling `text()`, `json()`, `arrayBuffer()` on `Request`/`Response` in workflows is automatically treated as a step:

```typescript
export async function handleWebhookWorkflow() {
  "use workflow";
  const webhook = createWebhook();
  const request = await webhook;
  
  const body = await request.json(); // Auto-cached as step
}
```

### Idempotency

Use `stepId` from `getStepMetadata()` as your idempotency key for external APIs:

```typescript
import { getStepMetadata } from "workflow";

async function chargeUser(userId: string, amount: number) {
  "use step";
  const { stepId } = getStepMetadata();
  
  await stripe.charges.create(
    { amount, currency: "usd", customer: userId },
    { idempotencyKey: stepId } // Stable across retries
  );
}
```

**Why this works:**
- `stepId` is stable across retries
- Globally unique per step
- Prevents duplicate charges/emails/operations

---

## How It Works

### Understanding Directives

Directives (`"use workflow"`, `"use step"`) provide the compile-time semantic boundary enabling durable execution.

**Why directives?** Alternatives explored:
1. **Runtime-only "Suspense"** - Required wrapping everything, unpredictable closures/mutations
2. **Generators** - Unfamiliar syntax, no sandboxing
3. **File system conventions** - Too opinionated, breaks code reuse
4. **Decorators** - Class-focused, presents workflows as runtime code when they're compile-time declarations
5. **Macro wrappers** - Same issue as decorators, whole-program analysis impractical

**What directives solve:**
- Compile-time semantic boundary
- Build-time validation (catch errors before deployment)
- No closure ambiguity (clear parameter passing)
- Natural async/await syntax
- Consistent syntax for both workflows and steps

### Code Transform

The compiler operates in three modes:

**Step Mode** (`.well-known/workflow/v1/step.js`):
```typescript
// Input
export async function createUser(email: string) {
  "use step";
  return { id: crypto.randomUUID(), email };
}

// Output
import { registerStepFunction } from "workflow/internal/private";

export async function createUser(email: string) {
  return { id: crypto.randomUUID(), email };
}

registerStepFunction("step//workflows/user.js//createUser", createUser);
```

**Workflow Mode** (`.well-known/workflow/v1/flow.js`):
```typescript
// Input
export async function handleUserSignup(email: string) {
  "use workflow";
  const user = await createUser(email);
  return { userId: user.id };
}

// Output
export async function createUser(email: string) {
  return globalThis[Symbol.for("WORKFLOW_USE_STEP")]("step//workflows/user.js//createUser")(email);
}

export async function handleUserSignup(email: string) {
  const user = await createUser(email);
  return { userId: user.id };
}
handleUserSignup.workflowId = "workflow//workflows/user.js//handleUserSignup";
```

**Client Mode** (your app code):
```typescript
// Input
export async function handleUserSignup(email: string) {
  "use workflow";
  const user = await createUser(email);
  return { userId: user.id };
}

// Output
export async function handleUserSignup(email: string) {
  throw new Error("Cannot execute workflow directly");
}
handleUserSignup.workflowId = "workflow//workflows/user.js//handleUserSignup";
```

**Why three modes?**
- **Step Mode:** Bundles executable steps with full runtime access
- **Workflow Mode:** Creates orchestration logic that replays from event logs
- **Client Mode:** Prevents direct execution, enables type-safe references

**ID format:** `{type}//{filepath}//{functionName}`
- Example: `workflow//workflows/user-signup.js//handleUserSignup`

**Generated files:**
- `flow.js` - Workflow execution (bundled, runs in Node.js VM for determinism)
- `step.js` - Step execution (full runtime access)
- `webhook.js` - Webhook delivery

### Framework Integrations

Build integrations by exposing three HTTP endpoints:

```typescript
// Build time: Generate handlers
import { BaseBuilder } from '@workflow/cli/dist/lib/builders/base-builder';

class MyFrameworkBuilder extends BaseBuilder {
  async build() {
    const inputFiles = await this.getInputFiles();
    
    await this.createWorkflowsBundle({
      outfile: '/.well-known/workflow/v1/flow.js',
      format: 'esm',
      inputFiles,
    });
    
    await this.createStepsBundle({
      outfile: '/.well-known/workflow/v1/step.js',
      format: 'esm',
      inputFiles,
    });
    
    await this.createWebhookBundle({
      outfile: '/.well-known/workflow/v1/webhook.js',
    });
  }
}

// Runtime: Add client mode transform
export function workflowPlugin() {
  return {
    name: 'workflow-client-transform',
    async transform(code, id) {
      if (!code.match(/(use step|use workflow)/)) return null;
      
      const result = await transform(code, {
        filename: id,
        jsc: {
          experimental: {
            plugins: [[require.resolve("@workflow/swc-plugin"), { mode: "client" }]],
          },
        },
      });
      
      return { code: result.code, map: result.map };
    },
  };
}

// HTTP Server: Expose endpoints
import flow from "./.well-known/workflow/v1/flow.js";
import step from "./.well-known/workflow/v1/step.js";
import * as webhook from "./.well-known/workflow/v1/webhook.js";

const server = Bun.serve({
  routes: {
    "/.well-known/workflow/v1/flow": { POST: (req) => flow.POST(req) },
    "/.well-known/workflow/v1/step": { POST: (req) => step.POST(req) },
    "/.well-known/workflow/v1/webhook/:token": webhook,
  },
});
```

**Security:** Handled by World abstraction (Vercel uses private queues, custom implementations use middleware/auth).

---

## Observability

```bash
# CLI
npx workflow inspect runs
npx workflow inspect runs --web  # Web UI

# Remote inspection (e.g., Vercel)
npx workflow inspect runs --backend vercel
```

---

## Deploying

### Worlds

A **World** connects workflows to infrastructure (storage, queuing, auth, streaming).

**Built-in worlds:**
- **Embedded World** - Filesystem-based for local development (`.workflow-data/`)
- **Vercel World** - Production-ready for Vercel deployments

**Default behavior:**
- Local dev: Automatically uses Embedded World
- Vercel: Automatically uses Vercel World

### Vercel World

**Features:**
- Scalable cloud storage
- Distributed queuing with auto-retry
- Token-based authentication
- Multi-environment (production/preview/dev)
- Team support

**Deploy:**
```bash
npx vercel deploy --prod  # Auto-configured
```

**Remote inspection:**
```bash
# Set environment variables
export WORKFLOW_TARGET_WORLD=vercel
export WORKFLOW_VERCEL_AUTH_TOKEN=<token>
export WORKFLOW_VERCEL_ENV=production
export WORKFLOW_VERCEL_PROJECT=<project-id>
export WORKFLOW_VERCEL_TEAM=<team-id>

# Or use CLI flags
npx workflow inspect runs \
  --backend=vercel \
  --env=production \
  --project=my-project \
  --team=my-team \
  --authToken=<token>
```

**API:**
```typescript
import { createVercelWorld } from '@workflow/world-vercel';

const world = createVercelWorld({
  token: process.env.WORKFLOW_VERCEL_AUTH_TOKEN,
  headers: {
    'x-vercel-environment': 'production',
    'x-vercel-project-id': 'my-project',
    'x-vercel-team-id': 'my-team',
  },
});
```

### Custom Worlds

Implement `World` interface with:
- **Storage** - Persist runs, steps, hooks, metadata
- **Queue** - Enqueue/process steps asynchronously
- **AuthProvider** - Handle API authentication
- **Streamer** - Manage readable/writable streams

See [World API Reference](/docs/deploying/world).
