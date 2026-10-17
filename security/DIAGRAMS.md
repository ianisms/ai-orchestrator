# Diagrams: attestation, token exchange, revocation

Current state is what runs in this repository today. Target state is the design the
prototypes are heading toward. Every "today" box names the file that implements it;
every gap names the threat in `THREAT_MODEL.md` it leaves open.

Status key: built = runs with tests; partial = runs but with a documented gap;
not built = design only.

## 1. Attestation today

Three kinds of principal reach the tool plane: the speaker device, the person
speaking, and the agents that act for them. Today each is trusted on a different
basis, and none of it is hardware-backed.

```mermaid
flowchart LR
    subgraph Device["Speaker device (Pi CM5)"]
        SPK[Speaker client<br/>speaker/modules/speaker/grpc_client.py]
    end

    subgraph Orch["Orchestrator (server/)"]
        PEER["gRPC peer address<br/>checked against trusted speaker nets<br/>voice_gate.py: _host_trusted"]
        SID["Speaker identification on this turn's audio<br/>identify_speaker, hidden from the model<br/>audio_tap.py"]
        LIVE["Liveness check<br/>NOT BUILT: liveness is None,<br/>so voice never verifies"]
        GATE["Voice gate<br/>voice_gate.py, monitor mode"]
    end

    subgraph Agents["Agent chain (security/delegation, prototype)"]
        CS["Client secret per agent<br/>sts.py: _authenticate"]
        ACT["Actor token<br/>client_credentials, 60 s"]
    end

    subgraph Tools["Tool servers"]
        VER["RS256 verify against STS key or JWKS<br/>verify.py"]
    end

    SPK -->|"network origin"| PEER
    SPK -->|"audio"| SID
    PEER --> GATE
    SID --> GATE
    LIVE -.->|"missing signal fails closed"| GATE
    CS --> ACT --> VER

    classDef gap fill:#fde2e2,stroke:#b00020,color:#000;
    classDef partial fill:#fff4d6,stroke:#b58100,color:#000;
    class LIVE gap;
    class PEER,CS,GATE partial;
```

What this means, in one line each:

- The device is trusted by where it is, not what it is. A trusted address is a
  network-origin check (`trusted_speaker_nets`), so a compromised host on that
  address passes (threat 18).
- The person is trusted by voice match plus origin. Liveness is not built, so in
  enforce mode no voice would verify; that is why the gate ships in monitor mode
  (threats 15, 21).
- Agents are trusted by a client secret. A secret is a bearer credential: whoever
  holds it is the agent (threat 6). Workload identity is prototype B, not built
  (threat 13).
- Tokens are not sender-constrained. A stolen tool token works until it expires,
  120 to 300 seconds (threat 1).

## 2. Attestation target

The device proves what it is with a key it cannot export; the agent proves what it
is with a workload identity issued from attestation; the person still authenticates
through the identity provider, with voice as a signal on top; and every token is
bound to the key of the thing that holds it.

```mermaid
flowchart LR
    subgraph Device["Speaker device"]
        TPM["Hardware-held device key<br/>TPM or secure element on the carrier board"]
        ENR["Enrollment: device key registered<br/>to a room and household"]
        DCRED["Short-lived device credential<br/>issued on attested start"]
    end

    subgraph Orch["Orchestrator"]
        MTLS["mTLS from device<br/>peer identity = device credential,<br/>not an address"]
        SID["Speaker identification on this turn's audio"]
        LIVE["Liveness challenge<br/>spoken challenge, then anti-spoof model"]
        STEP["Step-up on low confidence:<br/>approval on a registered phone"]
        GATE["Voice gate, enforce mode"]
    end

    subgraph Workloads["Agents and tools (prototype B)"]
        SPIRE["SPIRE server + agent<br/>node attestation (join token or TPM),<br/>workload attestation"]
        SVID["X.509-SVID per gateway, sub-agent, tool server<br/>replaces client secrets"]
        WMTLS["mTLS between gateway and tool servers"]
    end

    subgraph STS["Token service"]
        BIND["Tokens bound to the holder's key<br/>DPoP or mTLS-bound (RFC 8705)"]
    end

    TPM --> ENR --> DCRED --> MTLS --> GATE
    SID --> GATE
    LIVE --> GATE
    STEP -.-> GATE
    SPIRE --> SVID --> WMTLS
    SVID --> BIND
    DCRED --> BIND
```

What changes:

- "Where" becomes "what": the gRPC peer check becomes an mTLS peer identity from a
  key held in hardware. Closes threat 18 as a spoofing problem; a compromised device
  is then a revocation problem (section 4).
- The voice gate gets its missing signal. Liveness lands, then enforce mode (threats
  15, 21, rollout steps 1 to 4 in `THREAT_MODEL.md`).
- Agents get SVIDs instead of secrets. The actor credential at the STS becomes the
  SVID, and the policy decision gets the SPIFFE ID as the workload attribute
  (threats 6, 13).
- Tokens become sender-constrained. A stolen tool token is useless without the
  key (threat 1; "next controls" item 1).

The one-line version: today the speaker is trusted by network origin plus voice;
the target is a hardware-held device key, so the speaker proves what it is, not
just where it is.

## 3. Token exchange, as built (prototype A)

Runs in `security/delegation/`, 17 tests. The chain is user > mcp-gateway >
code-review-subagent, and access narrows at every hop.

```mermaid
sequenceDiagram
    autonumber
    participant U as User (alice)
    participant STS as STS<br/>sts.py
    participant GW as mcp-gateway
    participant SA as code-review-subagent
    participant T as tool-github<br/>verify.py
    participant PEP as PEP<br/>policy/ (prototype C)
    participant PDP as PDP (OPA)

    U->>STS: login + consent to mcp-gateway
    STS-->>U: user token<br/>aud=mcp-gateway, may_act=mcp-gateway, scope, 600 s
    U->>GW: user token

    GW->>STS: client_credentials
    STS-->>GW: actor token (token_use=actor, 60 s)
    GW->>STS: token exchange<br/>subject=user token, actor=actor token,<br/>delegate_to=code-review-subagent
    Note over STS: aud and may_act must name the actor<br/>scope = held AND actor AND next actor<br/>depth counted at handoff, cap 2
    STS-->>GW: handoff token<br/>aud=sub-agent, act=[gateway], may_act=sub-agent
    GW->>SA: handoff token

    SA->>STS: client_credentials
    STS-->>SA: actor token
    SA->>STS: token exchange<br/>subject=handoff, actor=actor token,<br/>audience=tool-github
    Note over STS: plain exchange of a token with act or may_act is refused<br/>exp = min(now + 120 s, subject exp), no refresh
    STS-->>SA: tool token<br/>sub=alice, act=[gateway, sub-agent], scope repo:read

    SA->>T: tools/call + tool token
    T->>T: verify signature, iss, aud, exp<br/>reject any token without act
    T->>PEP: subject (alice + act chain), action, resource, context
    PEP->>PDP: AuthZEN-shaped request
    Note over PDP: whole chain evaluated once<br/>every actor must hold the tool<br/>writes need the user's single-use approval
    PDP-->>PEP: decision + reasons (fail closed on timeout or error)
    PEP-->>T: allow / deny, logged
    T-->>SA: result or refusal
```

Rules the STS enforces on every exchange (each denial is logged with its reason):
delegation requires an actor token owned by the authenticated client; the subject's
`aud` and `may_act` must both name the actor; scope only narrows; audience and
`delegate_to` must be registered; chain depth is capped at 2; no refresh tokens.

Attacks run and refused: expired replay, exchange without `may_act`, another agent
presenting the user's token, sub-agent asking for `repo:write`, unregistered tool,
act-chain stripping, borrowed actor token, handoff token sent to a tool.

## 4. Revocation today and target

### Today

```mermaid
flowchart LR
    subgraph Today["Revocation today (prototype A and C)"]
        TTL["Short lifetimes only<br/>user 600 s, actor 60 s, tool 120 to 300 s<br/>no refresh tokens"]
        NOREV["No revocation endpoint<br/>no replay cache<br/>revoking the user token does not<br/>revoke exchanged tokens (threat 11)"]
        PDPX["PDP fail-closed is the only kill switch:<br/>OPA down or policy change = all calls denied<br/>(threat 9, by design)"]
        LOG["In-process audit log<br/>no tamper evidence, no central sink (threat 12)"]
    end
    TTL --> NOREV
    PDPX --> LOG
    classDef gap fill:#fde2e2,stroke:#b00020,color:#000;
    class NOREV,LOG gap;
```

Today the answer to "how do you revoke" is "wait up to five minutes, or pull the
policy." That is honest and it is not enough.

### Target

Revoke the narrowest thing that stops the harm, and make the cut immediate.

```mermaid
flowchart TD
    DETECT["Signal: injection seen, device compromised,<br/>user withdraws consent, agent retired"]

    DETECT --> SCOPE["1. Narrow the scope<br/>policy change at the PDP:<br/>remove the tool or action for this chain"]
    DETECT --> GRANT["2. Revoke the grant<br/>user's consent to this agent withdrawn<br/>at the STS; no new exchanges"]
    DETECT --> CRED["3. Revoke the credential<br/>agent SVID evicted in SPIRE;<br/>device key removed from the enrollment registry"]
    DETECT --> KILL["Immediate cut, any tier:<br/>gateway deny list by sub, act chain or jti,<br/>pushed to every PEP"]

    SCOPE --> AUDIT
    GRANT --> AUDIT
    CRED --> AUDIT
    KILL --> AUDIT
    AUDIT["Append-only audit sink:<br/>every revocation and every refused call,<br/>with the reason"]

    subgraph Binding["What makes it stick"]
        KB["Key-bound tokens: a revoked key<br/>kills every token it holds"]
        TTL2["Short lifetimes stay: revocation is for<br/>the minutes a token is still live"]
        CACHE["Speaker token cache: short TTL,<br/>cleared at session end,<br/>validated server-side on every call"]
    end
    KILL -.-> KB
    KILL -.-> TTL2
    KILL -.-> CACHE
```

Order of operations, narrowest first: scope, then grant, then credential. A gateway
deny-list entry is the immediate cut at any tier while the slower revocation lands.
Every hop is audited (threats 11, 12; "next controls" items 1, 2, 5).

## Where each piece lives

| Piece | Status | Where | Threats |
|---|---|---|---|
| Network-origin check for the speaker | built | `server/modules/orchestrator/core/voice_gate.py` | 18 |
| Speaker identification on the turn's audio | built | `server/modules/orchestrator/core/audio_tap.py`, identity tool | 16 |
| Liveness check | not built | rollout step 3 | 15, 21 |
| Spoken confirmation and phone approval | not built | rollout step 2 | 17, 19 |
| Hardware device key, enrollment registry, mTLS from device | not built | section 2 | 18 |
| Client-secret actor credentials | built (prototype) | `security/delegation/sts.py` | 6 |
| SPIFFE/SPIRE workload identity | not built (prototype B) | section 2 | 6, 13 |
| RFC 8693 exchange with nested `act` chain | built | `security/delegation/sts.py` | 2, 3, 4, 5 |
| Tool-side verifier | built | `security/delegation/verify.py` | 5, 10 |
| AuthZEN-shaped PEP and OPA PDP, fail closed | built | `security/policy/` | 7, 8, 9 |
| Sender-constrained tokens | not built | next controls item 1 | 1 |
| Revocation beyond lifetimes | not built | section 4 | 11 |
| Append-only audit sink | not built | next controls item 5 | 12 |
