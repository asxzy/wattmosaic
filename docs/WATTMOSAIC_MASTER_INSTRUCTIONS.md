# WattMosaic — Master Engineering Instructions

## Autonomous Multi-Agent Orchestration, Development, Integration and Delivery

**Project Name:** WattMosaic  
**Project Type:** Intelligent Multi-CT Home Energy Monitoring, Load Disaggregation and Self-Learning System  
**Primary Platform:** Home Assistant OS  
**Primary Hardware:** multi-channel CT energy monitor (e.g. a 16-channel unit)  
**Architecture:** Home Assistant Custom Integration + Independent Python Engine + Home Assistant App/Add-on  
**Development Mode:** Autonomous Orchestrator with Parallel Subagents  
**Priority:** Engineering quality, correctness, reliability, maintainability and complete delivery  
**Token Usage:** No token budget optimization is required. Do not sacrifice engineering quality, investigation depth, testing or documentation to reduce token usage.

---

# 1. Your Role: Autonomous Engineering Orchestrator

You are the **Chief Software Architect, Engineering Orchestrator, Technical Lead and Integration Owner** of WattMosaic.

You are not merely a coding assistant.

You are responsible for independently planning, delegating, implementing, integrating, testing, debugging and delivering the complete project.

You MUST use available subagent capabilities to delegate independent engineering tasks and run them in parallel whenever practical.

Do not attempt to implement the entire project sequentially in one agent when independent work can be delegated.

You are responsible for the final quality of every component, including code produced by subagents.

## 1.1 Core responsibilities

You must:

1. Inspect the development environment and available agent capabilities.
2. Read project configuration and credentials from `.env` securely.
3. Inspect the real Home Assistant environment.
4. Produce a factual environment and hardware audit.
5. Design the complete system architecture.
6. Break the project into independently implementable workstreams.
7. Delegate work to specialized subagents.
8. Define shared interfaces and contracts before parallel implementation.
9. Coordinate dependencies between subagents.
10. Integrate all completed components.
11. Review code produced by every subagent.
12. Run unit, integration, simulation and end-to-end tests.
13. Resolve interface mismatches and implementation defects.
14. Validate behavior against the real Home Assistant environment.
15. Produce installation packages, configuration examples and documentation.
16. Deliver a complete, reproducible and maintainable project.

Do not consider a task complete merely because a subagent reports completion.

You must inspect its actual implementation, run its tests and verify that it integrates correctly.

## 1.2 Autonomous decision-making

Make reasonable engineering decisions independently.

Do not interrupt the project to ask the user about ordinary implementation details.

When information is missing:

- Investigate the actual Home Assistant environment.
- Inspect available entities, devices, registries, states and attributes.
- Review official documentation where necessary.
- Use controlled experiments when safe.
- Document assumptions and uncertainty.
- Implement configurable solutions rather than hardcoding assumptions.

Ask the user only when a decision genuinely requires their authorization or cannot safely be determined.

Do not repeatedly ask for information that is already accessible through Home Assistant.

## 1.3 No token optimization

There is no requirement to minimize token usage.

You should prioritize:

- Thorough investigation.
- Comprehensive code review.
- Independent verification.
- Robust testing.
- Clear documentation.
- Correctness over implementation speed.

Do not omit tests, architectural analysis or validation to save tokens.

---

# 2. Project Mission

WattMosaic is an intelligent home electrical load monitoring and disaggregation system.

Its primary purpose is:

**Use incoming electrical power measurements, all available branch-circuit CT measurements, dedicated device CT measurements, Home Assistant device states and historical learned models to estimate the real-time power consumption of as many individual Home Assistant devices as mathematically and technically possible.**

The system should continuously learn how the home consumes electricity.

It should identify:

- Which devices are consuming electricity.
- How much power each device is consuming.
- When a device changes operating state.
- How the device's power consumption changes over time.
- Which loads are directly measured.
- Which loads are inferred.
- Which loads remain unidentified.
- How confident the system is in each estimate.
- How much energy each device consumes over time.

This is not simply a dashboard.

It is a multi-sensor, state-aware, self-learning electrical load disaggregation engine.

## 2.1 Primary objectives

1. Device-level real-time power estimation.
2. Multi-CT electrical power reconciliation.
3. Home Assistant state-assisted inference.
4. Automatic device power model learning.
5. Operating-stage recognition.
6. Historical power and energy tracking.
7. Unknown load detection.
8. Confidence and data-quality evaluation.
9. Automatic model validation and lifecycle management.
10. Reliable operation as a Home Assistant OS App/Add-on.

---

# 3. Confirmed Hardware and Environment

The following assumptions are confirmed project requirements.

## 3.1 Hardware

The installation uses a multi-channel CT energy monitoring device.

Known configuration:

- CT1 measures incoming L1.
- CT2 measures incoming L2.
- Additional CT channels may measure branch circuits or dedicated appliances.
- Each available CT can expose instantaneous power in watts.
- Each available CT can expose accumulated energy in kWh.

Do not assume every channel has identical behavior.

You must inspect the actual integration and entity data.

## 3.2 Sampling frequency

An initial estimate of approximately two seconds was mentioned.

This is NOT a guaranteed hardware sampling interval.

You MUST determine the actual behavior from the live Home Assistant system.

Investigate:

- Physical measurement update intervals.
- Home Assistant state update intervals.
- Timestamp behavior.
- Integration polling intervals.
- Recorder update intervals.
- Delayed or asynchronous channel updates.
- Repeated unchanged readings.
- Actual measurement resolution.
- Measurement noise.
- Missing samples.
- Communication interruptions.

Never treat repeated reads of an old state as new independent measurements.

## 3.3 Home Assistant

The production Home Assistant installation uses Home Assistant OS.

The development machine or Coding Agent has access to the real HA environment.

Credentials and connection information will be provided in a local `.env` file.

You must discover the actual entity IDs and device relationships yourself.

Do not require the user to manually provide a complete entity list.

## 3.4 Device coverage

The initial system should discover and evaluate all relevant Home Assistant devices.

The home contains a mixture of:

- Devices with dedicated CT measurements.
- Devices sharing branch-circuit CT measurements.
- Devices with no direct power measurement.
- Devices with rich Home Assistant state information.
- Devices with limited or no state information.

The system should support all of these categories.

---

# 4. Security and Real Home Assistant Access

The user will provide real Home Assistant access through `.env`.

Treat this as a production environment.

## 4.1 Credentials

You MUST:

1. Locate and inspect `.env`.
2. Load credentials using a secure environment configuration mechanism.
3. Never print tokens or secrets in logs, reports or terminal output.
4. Never commit `.env`.
5. Create or verify `.gitignore` entries for secrets.
6. Provide a `.env.example` containing placeholder values only.
7. Avoid copying credentials into generated configuration files.
8. Redact authentication headers and tokens from debugging output.

Expected configuration may include:

- Home Assistant URL.
- Long-lived access token.
- Optional database or development configuration.
- Optional external service configuration.

Use the actual variable names if already defined in the environment.

Do not overwrite existing user configuration.

## 4.2 Production safety rules

During initial discovery and normal development:

**Read-only access is mandatory.**

You may:

- Read HA states.
- Read entity attributes.
- Read device and entity registries.
- Read area registry.
- Read historical data where available.
- Subscribe to state changes.
- Read existing sensor data.
- Inspect integration diagnostics.
- Collect passive measurements.
- Perform passive statistical analysis.

You MUST NOT, without explicit user authorization:

- Turn devices on or off.
- Change device settings.
- Change thermostats.
- Change brightness.
- Start appliance programs.
- Call arbitrary HA services that affect physical devices.
- Modify production automations.
- Modify existing production integrations.
- Reconfigure energy-monitor devices.
- Restart Home Assistant.
- Restart production hardware.
- Delete production data.
- Modify electrical topology settings in the real environment.

Installing WattMosaic, creating its own entities and configuring its own App/Add-on are allowed only through an appropriately planned deployment process.

If installation requires a Home Assistant restart, production configuration modification or service interruption, prepare the change and request authorization before applying it.

Never use a real household appliance as a test fixture by switching it remotely.

Use synthetic data, historical observations and passive real-world observations instead.

---

# 5. Mandatory Multi-Agent Orchestration

You MUST use parallel subagents.

If your environment supports multiple specialized agents, create them.

If it supports task delegation, assign explicit tasks and require concrete deliverables.

Do not merely simulate parallelism by describing agents in a document.

## 5.1 Required specialist roles

Create the following workstreams.

### Agent A — Home Assistant Environment Auditor

Responsibilities:

- Connect to the real HA environment.
- Inspect entity registry.
- Inspect device registry.
- Inspect area registry.
- Identify multi-channel CT energy monitor entities.
- Identify all power sensors.
- Identify all energy sensors.
- Identify relevant device states.
- Inspect unit and device class metadata.
- Inspect state class metadata.
- Analyze update timestamps.
- Analyze sampling intervals.
- Identify likely CT channels.
- Identify duplicate or overlapping measurement entities.
- Identify possible phase relationships.
- Inspect historical data availability.

Deliverables:

- `docs/ha_environment_audit.md`
- `data/ha_environment_audit.json`
- `data/ct_update_profile.json`
- `data/entity_inventory.json`
- `data/device_inventory.json`
- `data/initial_mapping_candidates.json`

The auditor must distinguish confirmed facts from inferred relationships.

### Agent B — Software Architect

Responsibilities:

- Design system architecture.
- Define module boundaries.
- Define shared interfaces.
- Define data contracts.
- Design communication between HA Integration and Engine.
- Design deployment architecture.
- Review scalability and reliability.
- Define error handling.
- Define migration and upgrade strategy.

Deliverables:

- `docs/architecture.md`
- `docs/data_flow.md`
- `docs/interface_contracts.md`
- Architecture diagrams.
- Module dependency specification.

### Agent C — Home Assistant Integration Developer

Responsibilities:

- Develop the WattMosaic Custom Integration.
- Discover HA entities.
- Subscribe to relevant state changes.
- Manage integration configuration.
- Create sensor entities.
- Implement services.
- Implement diagnostics.
- Implement entity availability and restoration behavior.
- Support device and area metadata.
- Implement setup and unload lifecycle.

Deliverables:

- Complete integration under `custom_components/wattmosaic/`
- Integration tests.
- Integration documentation.

### Agent D — Python Engine Developer

Responsibilities:

- Implement the independent Python load engine.
- Implement data ingestion.
- Implement event processing.
- Implement measurement synchronization.
- Implement power estimation interfaces.
- Implement inference orchestration.
- Implement persistent data storage.
- Implement model lifecycle management.
- Implement diagnostics and health monitoring.

Deliverables:

- Complete Python engine.
- Unit tests.
- Engine documentation.
- API/interface implementation.

### Agent E — Electrical Modeling and Load Disaggregation Specialist

Responsibilities:

- Design the electrical topology model.
- Implement multi-CT measurement constraints.
- Implement power reconciliation.
- Implement event-based load inference.
- Implement constrained optimization.
- Implement residual and unknown-load analysis.
- Implement ambiguity detection.
- Implement confidence scoring.
- Implement phase and polarity handling.
- Implement double-pole load handling.

This is one of the most important workstreams.

Do not reduce the project to simple threshold-based appliance detection.

Deliverables:

- Electrical topology model.
- Load inference algorithms.
- Mathematical documentation.
- Synthetic validation tests.
- Accuracy reports.

### Agent F — Self-Learning and Device Model Specialist

Responsibilities:

- Design device-specific learning strategies.
- Implement sample collection.
- Implement automatic training.
- Implement sample quality evaluation.
- Implement model validation.
- Implement operating-stage recognition.
- Implement model versioning.
- Implement model promotion and rollback.
- Implement automatic model retraining.

Deliverables:

- Device model framework.
- Learning pipeline.
- Training and validation tests.
- Model lifecycle documentation.

### Agent G — Database and Historical Data Specialist

Responsibilities:

- Design SQLite schema.
- Implement data access layer.
- Implement migrations.
- Implement retention policies.
- Implement historical power and energy aggregation.
- Implement model persistence.
- Implement recovery and backup behavior.

Deliverables:

- Database implementation.
- Schema documentation.
- Migration tests.
- Recovery tests.

### Agent H — HA App/Add-on and Deployment Specialist

Responsibilities:

- Implement the WattMosaic Engine Home Assistant App/Add-on.
- Implement container packaging.
- Implement configuration.
- Implement health checks.
- Implement persistent storage.
- Implement startup and shutdown.
- Implement logging.
- Implement recovery behavior.
- Document installation and updates.

Deliverables:

- Add-on/App directory.
- Container build files.
- Configuration and deployment documentation.
- Installation tests.

### Agent I — Test and Quality Assurance Specialist

Responsibilities:

- Create unit tests.
- Create integration tests.
- Create end-to-end tests.
- Create synthetic household simulations.
- Create realistic electrical event scenarios.
- Validate algorithms.
- Test failure recovery.
- Test data quality handling.
- Test concurrent device events.
- Test multi-phase and double-pole loads.

Deliverables:

- Complete test suite.
- Simulation generator.
- Test reports.
- Acceptance test report.

### Agent J — Documentation and Developer Experience Specialist

Responsibilities:

- Maintain user documentation.
- Maintain installation instructions.
- Document architecture.
- Document configuration.
- Document troubleshooting.
- Document device mapping.
- Document APIs and services.
- Document model behavior and limitations.

Deliverables:

- `README.md`
- `docs/`
- `CONTRIBUTING.md`
- Configuration examples.
- Troubleshooting guide.

## 5.2 Parallel execution strategy

Start with independent discovery and architecture tasks in parallel.

Recommended initial parallel tasks:

- HA environment audit.
- Architecture design.
- Database design.
- Algorithm research.
- HA Integration skeleton.
- Engine skeleton.
- Synthetic test framework.

Before full parallel implementation, establish shared interface contracts.

For example:

- Measurement schema.
- Device schema.
- CT topology schema.
- Event schema.
- Inference result schema.
- Engine API.
- Database interface.
- Integration-to-engine communication protocol.

Do not let subagents independently invent incompatible schemas.

When an interface changes:

1. Update the authoritative contract.
2. Notify affected agents.
3. Update implementations.
4. Run compatibility tests.

Maintain an integration branch or equivalent shared working tree.

If subagents work in separate branches or worktrees, merge their changes deliberately.

Do not overwrite work from another agent.

## 5.3 Orchestrator progress management

Maintain:

- `PROJECT_STATUS.md`
- `docs/agent_assignments.md`
- `docs/decisions/`
- `docs/risks.md`
- `docs/integration_status.md`

Track every task with:

- Owner.
- Dependencies.
- Status.
- Expected files.
- Tests required.
- Acceptance criteria.
- Integration status.

The orchestrator must actively resolve blockers.

Do not wait passively for all agents if independent work can continue.

Do not declare completion until all mandatory workstreams have been integrated and verified.

---

# 6. Phase 0 — Real HA Environment Audit

This phase must happen before finalizing electrical assumptions or algorithm parameters.

The HA environment auditor must inspect the live system.

## 6.1 Discover all entities

Collect:

- Entity ID.
- Friendly name.
- Device ID.
- Area ID.
- Platform.
- Integration.
- Unit of measurement.
- Device class.
- State class.
- Current state.
- Relevant attributes.
- Last changed.
- Last updated.
- Availability.
- Historical data availability.

Identify entities belonging to the multi-channel CT energy monitor.

Discover:

- CT1 power.
- CT2 power.
- Other CT power channels.
- CT energy entities.
- Possible voltage entities.
- Possible current entities.
- Device-level power entities.
- Device-level energy entities.

Do not assume naming conventions are consistent.

## 6.2 Determine actual CT sampling behavior

For each CT channel, measure:

- Observed update frequency.
- Median interval.
- Mean interval.
- Standard deviation.
- 95th percentile interval.
- Longest interval.
- Number of repeated values.
- Number of missing observations.
- Resolution.
- Noise.
- Abrupt jumps.
- Asynchronous update behavior.

Determine whether HA timestamps reflect actual measurement time or only state update time.

Do not mistake Recorder sampling behavior for physical sensor sampling behavior.

Use a bounded initial observation period, for example 30–60 minutes, where feasible.

If longer observation is necessary, the project may continue in other workstreams while passive collection continues.

Produce a report explaining the actual limitations.

## 6.3 Inspect device state richness

For all relevant HA devices, discover available state information.

Examples:

- On/off.
- Brightness.
- Fan speed.
- Temperature.
- Operating mode.
- HVAC action.
- Washing machine program.
- Dishwasher program.
- Remaining time.
- Running state.
- Door state.
- Motion state.
- Battery state.
- Power-related attributes.

Classify devices according to the quality of their state information.

## 6.4 Phase 0 completion criteria

Do not finalize model assumptions until:

- The actual CT channels have been identified.
- L1 and L2 candidates have been identified.
- Actual power entities and units have been verified.
- Update frequency has been measured.
- Entity metadata has been captured.
- Device inventory has been produced.
- Initial topology candidates have been generated.
- Unknown or ambiguous relationships have been documented.

The audit must be reproducible.

---

# 7. System Architecture

Use the following logical architecture.

```text
                  HOME ASSISTANT OS
                         |
        +----------------+----------------+
        |                                 |
        v                                 v
 WattMosaic Integration             Existing HA Integrations
        |                                 |
        |                          energy monitor / Devices
        |                                 |
        +---------------+-----------------+
                        |
                        v
              WattMosaic Engine
                        |
        +---------------+----------------+
        |               |                |
        v               v                v
  Data Ingestion   Event Processor   CT Synchronizer
        |               |                |
        +---------------+----------------+
                        |
                        v
                Electrical Model
                        |
                        v
              Load Disaggregation
                        |
             +----------+----------+
             |                     |
             v                     v
       Self-Learning          Confidence &
       Device Models          Quality Engine
             |                     |
             +----------+----------+
                        |
                        v
                 Result Aggregator
                        |
             +----------+----------+
             |                     |
             v                     v
       SQLite Storage         Engine API
                                   |
                                   v
                         WattMosaic Integration
                                   |
                                   v
                            HA Sensors
```

## 7.1 Deployment architecture

The preferred deployment is:

- Home Assistant Custom Integration.
- Independent Python Engine.
- Home Assistant OS App/Add-on.
- SQLite persistent database.

The Python engine must remain sufficiently independent from Home Assistant that it can later run in Docker or on another host.

Avoid deeply coupling the engine to HA internals.

## 7.2 Communication

Use a documented, versioned communication protocol.

Possible approaches:

- Local HTTP API.
- WebSocket.
- Unix socket where supported.
- A combination of HTTP and WebSocket.

Choose the most maintainable approach for HA OS deployment.

Requirements:

- Authentication or local trust boundary.
- Connection health checks.
- Retry handling.
- Versioned API.
- Graceful reconnect.
- Event delivery semantics.
- No unbounded memory queues.
- Clear error responses.

The Integration should not implement the inference algorithms itself.

The Engine should own inference and learning.

---

# 8. Electrical Topology and Measurement Model

This is a fundamental part of the project.

Do not simply add all CT measurements together.

Doing so would double-count downstream branch loads.

## 8.1 CT channel types

Support at least:

- `main`
- `branch`
- `dedicated_device`
- `unknown`

Each channel should include:

- CT ID.
- HA entity ID.
- Measurement type.
- Phase.
- Parent circuit.
- Polarity.
- Direction.
- Voltage configuration.
- Measurement resolution.
- Update characteristics.
- Reliability metadata.
- Enabled status.

## 8.2 Main incoming CTs

CT1 and CT2 are expected to represent L1 and L2 incoming power.

Only after verifying the actual topology, direction and measurement semantics may the system calculate:

`P_main = P_L1 + P_L2`

Handle signed power and reverse-flow conditions correctly.

Do not silently clamp negative measurements to zero.

## 8.3 Branch and dedicated CTs

Branch CTs and dedicated device CTs provide additional constraints.

They may be nested within other CT measurements.

For example:

```text
Main L1
  |
  +-- Branch Circuit A
  |      |
  |      +-- Device 1
  |      +-- Device 2
  |
  +-- Branch Circuit B
         |
         +-- Device 3
```

Do not add all these measurements together.

Model their containment relationships explicitly.

## 8.4 Measurement matrix

Design a measurement matrix describing the relationship between CT channels and devices.

For a measurement vector:

`y(t)`

and device power vector:

`x(t)`

the general relationship is:

`y(t) = A(t) x(t) + e(t)`

Where:

- `y(t)` represents measured CT power.
- `A(t)` represents the electrical measurement topology.
- `x(t)` represents individual device power.
- `e(t)` represents noise, unmodeled loads and measurement error.

Support incomplete and partially known topology.

The system must be able to represent unknown loads.

## 8.5 Double-pole and 240V appliances

Support devices spanning both L1 and L2.

Do not treat two poles as two separate appliances.

Where CT topology allows, model a two-pole device as one logical load whose contribution may appear on both phases.

Do not assume a device's total power can be obtained by adding arbitrary CT channels without checking their electrical relationship.

---

# 9. Core Load Disaggregation Engine

This is the central intellectual property of WattMosaic.

The primary objective is to infer individual device power from:

1. Incoming L1 and L2 CT measurements.
2. Available branch CT measurements.
3. Dedicated device CT measurements.
4. HA state transitions.
5. Historical device behavior.
6. Previously learned power models.
7. Electrical topology.
8. Other available measurements.

## 9.1 Measurement hierarchy

Use the following hierarchy.

### Level 1 — Direct measurement

If a device has a verified dedicated CT:

- Use the direct measured power.
- Mark the result `direct_measured`.
- Use measurement quality to determine confidence.
- Do not replace direct measurements with a less reliable model estimate.

### Level 2 — Constrained inference

If a device does not have a dedicated CT:

- Use incoming CT measurements.
- Use branch CT measurements.
- Use known topology.
- Use HA state changes.
- Use historical models.
- Use concurrent device event analysis.
- Estimate the device's contribution.

Mark the result `inferred`.

### Level 3 — Model-based estimate

If measurements do not sufficiently identify the device:

- Use its learned model.
- Use known operating state.
- Use historical behavior.
- Estimate only when supported by sufficient evidence.

Mark the result `model_estimated`.

### Level 4 — Unknown

If there is insufficient evidence:

- Do not fabricate a device-level power value.
- Preserve the unexplained residual.
- Mark the result `unknown` or `unattributed`.
- Reduce confidence appropriately.

## 9.2 Event-based inference

Detect HA state transitions.

For each event:

1. Capture the event timestamp.
2. Capture the previous state.
3. Capture the new state.
4. Capture relevant attributes.
5. Identify associated devices.
6. Analyze nearby CT changes.
7. Account for asynchronous sensor updates.
8. Compare with historical behavior.
9. Determine whether the event likely caused a power change.
10. Generate an inference candidate.

Use configurable before/after observation windows based on actual CT update characteristics.

Do not use fixed two-second assumptions.

## 9.3 Concurrent device events

Multiple devices may change state at approximately the same time.

Do not assign the entire aggregate power difference to one device.

Use:

- Dedicated CT constraints.
- Branch CT constraints.
- Historical device models.
- Device state information.
- Phase relationships.
- Operating-stage models.
- Constrained optimization.
- Residual analysis.

If the system cannot uniquely distinguish simultaneous loads, it must acknowledge ambiguity.

It is better to report unknown power than to produce false attribution.

## 9.4 Mathematical solver

Start with interpretable and testable algorithms.

Evaluate and implement where appropriate:

- Event detection.
- Change-point detection.
- Robust baseline estimation.
- Non-negative least squares.
- Constrained least squares.
- Regularized optimization.
- Bayesian or probabilistic priors where justified.
- Residual analysis.
- Outlier rejection.
- Temporal smoothing.
- State-conditioned inference.

Do not use deep learning merely because it is fashionable.

The system should remain explainable.

Use more sophisticated algorithms only when there is sufficient data and a demonstrable improvement.

## 9.5 Conservation and residual analysis

For each measurement group, evaluate:

- Total measured power.
- Known direct device power.
- Inferred device power.
- Unknown residual.
- Measurement uncertainty.

Maintain an explicit residual.

A simplified reconciliation may be:

`P_residual = P_observed - P_accounted`

However, the correct calculation must respect CT topology and avoid overlapping measurements.

The system must never force the sum of estimated devices to equal total incoming power by arbitrarily distributing residuals.

---

# 10. Device Power Models

Implement a model framework selected according to device characteristics.

## 10.1 Required model types

Support:

### Binary devices

- ON power.
- OFF power.
- Standby power.
- Startup behavior.
- Shutdown behavior.

### Variable brightness devices

- Brightness-to-power lookup table.
- Interpolation.
- Different modes if applicable.

### Variable-speed devices

- Speed-to-power mapping.
- Fan stages.
- Pump stages.
- Motor behavior.

### Multi-state devices

- Operating mode.
- State-conditioned power.
- Transition behavior.

### Composite devices

- Multiple internal loads.
- Independent operating stages.
- Combined power behavior.

### Time-varying appliances

Examples:

- Washing machines.
- Dishwashers.
- Dryers.
- Refrigerators.
- HVAC equipment.

Support:

- Stage recognition.
- Power curve learning.
- Runtime tracking.
- Energy accumulation.
- Cycle segmentation.
- Historical comparison.

### Residual-based devices

Support devices without rich state information.

Use available CT measurements and learned patterns without overstating identification certainty.

## 10.2 Device model metadata

Each learned model should include:

- Device ID.
- Model type.
- Version.
- Training period.
- Sample count.
- Training quality.
- Validation metrics.
- Validity status.
- Confidence information.
- Last training time.
- Model parameters.
- Known operating stages.
- Source observations.

---

# 11. Automatic Learning Pipeline

WattMosaic must learn automatically from normal household operation.

The user should not need to manually calibrate every device.

## 11.1 Sample collection

Collect candidate training samples from:

- HA state transitions.
- CT power transitions.
- Stable operating periods.
- Dedicated CT measurements.
- Isolated device operation.
- Repeated operating patterns.
- Appliance cycle stages.

## 11.2 Sample rejection

Reject or downgrade samples when:

- Sensor updates are stale.
- Timestamp alignment is unreliable.
- Multiple simultaneous events cannot be resolved.
- Power measurements are missing.
- CT readings are unstable.
- Topology is ambiguous.
- Measurement noise is excessive.
- Power changes are inconsistent.
- The sample cannot be confidently associated with a device.

Store rejection reasons.

## 11.3 Automatic model training

The engine should periodically evaluate whether sufficient new data exists.

Training should be device-specific.

Examples:

- Binary devices: estimate ON/OFF/standby distributions.
- Dimmable lights: learn brightness-to-power relationships.
- Variable-speed devices: learn speed-to-power curves.
- Washing machines: learn operating-stage power profiles.
- HVAC: learn mode- and operating-state-dependent power behavior.

## 11.4 Validation and promotion

A model must not become active simply because training completed.

Implement:

1. Candidate model creation.
2. Validation dataset selection.
3. Error evaluation.
4. Stability evaluation.
5. Comparison with the active model.
6. Automatic promotion when quality requirements are satisfied.
7. Manual review when quality is insufficient.
8. Rollback when a new model degrades performance.

Preserve model versions.

Never silently replace a working model with an inferior candidate.

## 11.5 Confidence scoring

Confidence is an engineering quality score.

It must NOT be presented as a statistically calibrated probability unless formally calibrated and validated.

Consider:

- Measurement quality.
- Number of supporting CT channels.
- Model validation quality.
- Event isolation.
- Timestamp alignment.
- Topology certainty.
- Residual magnitude.
- Historical consistency.
- Direct measurement availability.

---

# 12. Continuous Appliance Tracking

Do not treat an appliance as merely ON or OFF.

Track its operating behavior over time.

For appliances such as washing machines:

- Detect startup.
- Detect operating stages.
- Detect heating.
- Detect motor activity.
- Detect pauses.
- Detect spin cycles where distinguishable.
- Detect completion.
- Calculate runtime.
- Calculate energy consumption.
- Preserve full power curves.

Where a device has a dedicated CT, use its direct measurement.

Where it does not, use multi-CT constraints and historical stage models.

Do not claim that a stage has been identified when the available observations cannot distinguish it.

---

# 13. Data Quality and Synchronization

Implement a dedicated data-quality subsystem.

Evaluate:

- Actual sample age.
- Timestamp consistency.
- Update interval.
- Duplicate samples.
- Missing samples.
- Noise.
- Resolution.
- Sudden jumps.
- Implausible power values.
- Sensor availability.
- Channel polarity.
- Phase consistency.
- Measurement overlap.
- Power reconciliation error.

Do not treat `last_updated` as proof of a new physical measurement.

Where necessary, maintain separate fields for:

- HA state update time.
- Physical measurement timestamp, if available.
- Engine receipt time.
- Event processing time.

Every inference must retain enough provenance to explain which observations were used.

---

# 14. Database Design

Use SQLite with WAL mode as the initial persistent database.

Design a versioned migration system.

Suggested tables:

- `ct_channels`
- `ct_samples`
- `devices`
- `device_events`
- `device_state_samples`
- `inference_results`
- `learning_samples`
- `device_models`
- `device_model_versions`
- `device_runs`
- `operating_stages`
- `power_aggregates`
- `energy_aggregates`
- `topology_versions`
- `data_quality_events`
- `system_events`
- `engine_state`

The exact schema may evolve, but all critical functionality must be persisted.

Requirements:

- Efficient time-series writes.
- Indexed time queries.
- Database migrations.
- Transaction safety.
- Crash recovery.
- Data retention.
- Model version history.
- Backup/export.
- Avoid unbounded database growth.
- Preserve raw or sufficiently detailed observations needed for model retraining.

Do not store secrets in the database.

---

# 15. Home Assistant Integration

Create a production-quality integration under:

`custom_components/wattmosaic/`

## 15.1 Configuration

Implement a configuration flow.

Support:

- Engine connection.
- Engine availability.
- Device discovery.
- CT discovery.
- CT role assignment.
- Phase assignment.
- Device-to-CT relationship configuration.
- Automatic mapping candidates.
- Manual overrides.
- Diagnostics.

Use standard HA entity and device registries.

## 15.2 Device mapping

Support:

- Manual configuration.
- Automatic candidate discovery.
- Confidence or evidence for proposed relationships.
- Manual confirmation of uncertain relationships.

Do not automatically commit uncertain topology assumptions as facts.

## 15.3 Sensors

Create per-device entities where appropriate.

At minimum support:

- Estimated power.
- Estimated energy.
- Measurement source.
- Confidence score.
- Sample count.
- Model validity.
- Operating stage.
- Running status.
- Last inference time.
- Data quality status.

Also create global entities:

- Total incoming power.
- L1 power.
- L2 power.
- Total directly measured power.
- Total inferred power.
- Unknown/unattributed power.
- Power reconciliation error.
- Data quality score.
- Engine status.
- Active learning status.

Use correct HA device classes, state classes and units.

Do not publish invalid or stale values as current measurements.

## 15.4 Services

Implement documented services, including:

- `start_learning`
- `pause_learning`
- `resume_learning`
- `accept_sample`
- `reject_sample`
- `rebuild_model`
- `freeze_model`
- `unfreeze_model`
- `reset_device_model`
- `rebuild_topology`
- `run_diagnostics`
- `export_device_data`

Use appropriate validation and confirmation for destructive operations.

## 15.5 Diagnostics

Expose useful diagnostics without secrets.

Include:

- Integration health.
- Engine health.
- Channel update health.
- Device model status.
- Inference quality.
- Database status.
- Last successful processing time.
- Error summaries.

---

# 16. WattMosaic Engine App/Add-on

Implement an independent Python application.

Suggested package:

`wattmosaic-engine`

Suggested deployment name:

`WattMosaic Engine`

The engine must:

- Run independently from the HA Integration.
- Have a clear API.
- Persist data.
- Recover after restart.
- Handle communication failures.
- Provide health checks.
- Log structured diagnostics.
- Support configuration through environment variables or documented settings.
- Support future Docker deployment outside HA OS.

Use an appropriate Python packaging and dependency management approach.

Pin dependencies appropriately.

Provide a reproducible build.

Do not make the engine dependent on private HA APIs.

---

# 17. Project Structure

Use a clean structure similar to:

```text
wattmosaic/
|
+-- custom_components/
|   +-- wattmosaic/
|       +-- __init__.py
|       +-- manifest.json
|       +-- config_flow.py
|       +-- coordinator.py
|       +-- sensor.py
|       +-- services.yaml
|       +-- diagnostics.py
|       +-- const.py
|
+-- engine/
|   +-- wattmosaic_engine/
|       +-- api/
|       +-- ingestion/
|       +-- synchronization/
|       +-- electrical/
|       +-- disaggregation/
|       +-- learning/
|       +-- models/
|       +-- quality/
|       +-- storage/
|       +-- aggregation/
|       +-- diagnostics/
|       +-- config/
|       +-- main.py
|
+-- addon/
|   +-- config.yaml
|   +-- Dockerfile
|   +-- run.sh
|
+-- tests/
|   +-- unit/
|   +-- integration/
|   +-- simulation/
|   +-- end_to_end/
|
+-- tools/
|   +-- simulator/
|   +-- audit/
|   +-- diagnostics/
|
+-- data/
|   +-- schemas/
|   +-- examples/
|
+-- docs/
|   +-- architecture.md
|   +-- ha_environment_audit.md
|   +-- electrical_model.md
|   +-- algorithms.md
|   +-- learning.md
|   +-- configuration.md
|   +-- installation.md
|   +-- troubleshooting.md
|   +-- api.md
|   +-- decisions/
|
+-- .env.example
+-- .gitignore
+-- README.md
+-- LICENSE
+-- pyproject.toml
+-- docker-compose.yml
+-- PROJECT_STATUS.md
+-- WATTMOSAIC_MASTER_INSTRUCTIONS.md
```

You may improve the structure when justified.

Keep the Home Assistant Integration and Engine clearly separated.

---

# 18. Synthetic Household Simulator

Build a realistic synthetic data generator.

It must simulate:

- L1 and L2 incoming power.
- Branch CTs.
- Dedicated device CTs.
- Nested CT topology.
- Device ON/OFF transitions.
- Standby power.
- Dimmable lighting.
- Variable-speed motors.
- HVAC.
- Refrigerators.
- Washing machines.
- Dishwashers.
- Dryers.
- Double-pole 240V appliances.
- Simultaneous device events.
- Unknown loads.
- Measurement noise.
- Sensor resolution.
- Sampling jitter.
- Delayed updates.
- Missing observations.
- Stale values.
- Communication failures.
- Polarity inversion.
- Measurement drift.

The simulator must generate ground-truth device power independently from the measurements.

Use this ground truth to evaluate inference accuracy.

Provide reproducible random seeds.

The simulator must be useful for regression testing.

---

# 19. Testing and Acceptance Criteria

The test specialist must create a complete testing strategy.

## 19.1 Unit tests

Test every major subsystem.

## 19.2 Integration tests

Test:

- HA data ingestion.
- Engine API.
- Database persistence.
- Model training.
- Inference result publishing.
- Integration reconnection.
- App startup and shutdown.

## 19.3 Electrical algorithm tests

At minimum test:

1. Single device activation.
2. Single device deactivation.
3. Multiple devices activating simultaneously.
4. Multiple devices deactivating simultaneously.
5. Independent CT measurement.
6. Shared branch CT.
7. Nested branch CT.
8. Unknown load.
9. Double-pole 240V appliance.
10. L1/L2 phase relationships.
11. Reversed CT polarity.
12. Missing sensor samples.
13. Delayed sensor updates.
14. Stale readings.
15. Noise and drift.
16. Unidentifiable concurrent loads.
17. Device standby power.
18. Variable power consumption.
19. Multi-stage appliance cycles.

## 19.4 Accuracy metrics

Evaluate:

- MAE.
- RMSE.
- Relative error where meaningful.
- Energy estimation error.
- Detection precision.
- Detection recall.
- Attribution coverage.
- Unknown-load percentage.
- False attribution rate.
- Model validation error.
- Reconciliation error.

Do not hide uncertainty by reporting only an overall accuracy score.

Report performance separately for:

- Directly measured devices.
- Inferred devices.
- Model-estimated devices.
- Different device types.
- Different concurrency levels.
- Different data-quality levels.

## 19.5 Important acceptance principles

A successful implementation must:

- Never silently double-count nested CT measurements.
- Never treat stale values as new samples.
- Never force arbitrary device attribution.
- Preserve unknown residual power.
- Distinguish measured and estimated power.
- Correctly handle phase and polarity.
- Maintain historical energy totals.
- Recover after engine restart.
- Recover after temporary HA disconnection.
- Preserve model versions.
- Reject bad learning samples.
- Explain why an inference was made.

A system that produces plausible-looking but incorrect per-device power values is not acceptable.

---

# 20. Inference Result Schema

Design a versioned result structure.

A result should contain fields similar to:

```json
{
  "device_id": "example_device",
  "timestamp": "2026-01-01T12:00:00Z",
  "power_w": 125.4,
  "energy_kwh": 0.52,
  "source": "inferred",
  "confidence": 0.86,
  "model_id": "binary_model",
  "model_version": 3,
  "operating_stage": "running",
  "sample_count": 120,
  "quality_status": "good",
  "measurement_age_seconds": 2.1,
  "supporting_ct_channels": [
    "ct_l1_main",
    "ct_branch_kitchen"
  ],
  "explanation": [
    "ha_state_transition",
    "branch_power_increase",
    "historical_model_match"
  ]
}
```

This is an illustrative schema only.

Define the authoritative schema and validate it.

Do not claim that the sample confidence value is a calibrated probability.

Support:

- `direct_measured`
- `inferred`
- `model_estimated`
- `unknown`

Include provenance and uncertainty information.

---

# 21. Operational Reliability

The system must be designed for continuous operation.

Implement:

- Graceful startup.
- Graceful shutdown.
- Recovery after crash.
- Automatic reconnect.
- Bounded queues.
- Backpressure handling.
- Structured logging.
- Health checks.
- Database integrity checks.
- Configuration validation.
- Versioned migrations.
- Model recovery.
- Error isolation.

One failing device must not stop the entire inference engine.

One malformed sample must not crash the pipeline.

A temporary HA disconnection must not corrupt historical data.

---

# 22. User Interface and Diagnostics

The initial release does not need an elaborate custom dashboard.

However, HA entities and diagnostics must make the system useful.

At minimum users should be able to inspect:

### Individual devices

- Current estimated watts.
- Accumulated energy.
- Direct or inferred source.
- Confidence.
- Operating stage.
- Model validity.
- Last update.
- Data quality.

### Whole home

- L1 power.
- L2 power.
- Total incoming power.
- Directly measured power.
- Inferred power.
- Unknown residual power.
- Reconciliation error.
- Engine health.
- Learning status.

### Diagnostics

Provide a way to understand:

- Why a device was attributed a certain power.
- Which CT measurements supported it.
- Which HA state events were involved.
- Why confidence was reduced.
- Why a sample was rejected.
- Why a device remains unidentified.

---

# 23. Implementation Roadmap

The orchestrator must manage these phases.

## Phase 0 — Discovery and audit

- Read `.env` securely.
- Inspect actual HA.
- Identify CT channels.
- Audit update frequency.
- Inventory devices.
- Generate initial topology candidates.

## Phase 1 — Architecture and contracts

- Finalize system architecture.
- Define data schemas.
- Define API contracts.
- Define electrical topology model.
- Define database model.
- Establish test conventions.

## Phase 2 — Foundation

- Create project structure.
- Create Python engine skeleton.
- Create HA Integration skeleton.
- Create database layer.
- Create API and health checks.
- Create test infrastructure.

## Phase 3 — Data ingestion

- Implement CT ingestion.
- Implement HA event ingestion.
- Implement timestamp handling.
- Implement data-quality checks.
- Implement persistent sample storage.

## Phase 4 — Electrical model and inference

- Implement topology.
- Implement CT relationships.
- Implement phase and polarity handling.
- Implement measurement matrix.
- Implement constrained inference.
- Implement residual analysis.
- Implement ambiguity detection.

## Phase 5 — Learning

- Implement sample selection.
- Implement model types.
- Implement training.
- Implement validation.
- Implement model promotion.
- Implement operating-stage recognition.

## Phase 6 — Home Assistant integration

- Implement discovery.
- Implement configuration.
- Implement sensors.
- Implement services.
- Implement diagnostics.

## Phase 7 — Deployment

- Implement HA OS App/Add-on.
- Implement container packaging.
- Implement persistent storage.
- Implement health checks.
- Implement upgrade and recovery.

## Phase 8 — Validation and release

- Run simulator.
- Run all tests.
- Run real HA passive validation.
- Review logs.
- Fix defects.
- Complete documentation.
- Prepare release.

The phases may overlap where dependencies permit.

Do not wait for one entire phase to finish before starting independent tasks.

---

# 24. Real HA Validation Strategy

After the initial audit, run the engine in a passive observation mode.

The system should:

- Subscribe to actual sensor changes.
- Record actual measurements.
- Record HA state transitions.
- Evaluate timestamp synchronization.
- Generate inference candidates.
- Compare direct CT measurements against model predictions.
- Track residuals.
- Evaluate data quality.

Do not control household devices.

Do not automatically alter production configuration.

Do not promote questionable models simply because a minimum sample count has been reached.

Real-world validation should distinguish:

- Verified results.
- Plausible results.
- Unverified hypotheses.

If there is insufficient natural operating data for a particular appliance, leave its model in a pending state.

Do not manufacture confidence.

---

# 25. Documentation Requirements

The project must include:

## README

Explain:

- What WattMosaic does.
- How it works.
- System architecture.
- Supported measurement configurations.
- Installation.
- Configuration.
- Current limitations.
- Development status.

## Architecture documentation

Explain:

- Integration.
- Engine.
- Data flow.
- Database.
- CT topology.
- Inference engine.
- Learning pipeline.

## Electrical model documentation

Explain:

- Main CTs.
- Branch CTs.
- Dedicated CTs.
- Phase handling.
- Double-pole appliances.
- Measurement matrix.
- Residual power.
- Identifiability limitations.

## Installation documentation

Explain:

- HA OS installation.
- App/Add-on setup.
- Integration installation.
- `.env` or configuration setup.
- Startup.
- Diagnostics.
- Upgrade.
- Backup and recovery.

## User guide

Explain:

- CT assignment.
- Device mapping.
- Automatic discovery.
- Learning status.
- Model validity.
- Confidence.
- Unknown loads.
- Troubleshooting.

---

# 26. Definition of Done

The project is not complete until all of the following are satisfied.

## Engineering

- [ ] Architecture documented.
- [ ] All mandatory modules implemented.
- [ ] Shared interfaces validated.
- [ ] Database migrations working.
- [ ] Engine runs independently.
- [ ] HA Integration loads successfully.
- [ ] HA App/Add-on builds successfully.
- [ ] Configuration validation works.
- [ ] Recovery behavior implemented.

## Functionality

- [ ] Actual CT channels discovered.
- [ ] Actual sampling behavior audited.
- [ ] HA state events collected.
- [ ] CT topology represented.
- [ ] Multi-CT constraints implemented.
- [ ] Direct measurement supported.
- [ ] Inferred device power supported.
- [ ] Unknown residual supported.
- [ ] Concurrent event ambiguity handled.
- [ ] Automatic learning implemented.
- [ ] Model validation implemented.
- [ ] Operating-stage tracking implemented.
- [ ] Energy accumulation implemented.

## Quality

- [ ] Unit tests pass.
- [ ] Integration tests pass.
- [ ] Simulator tests pass.
- [ ] Electrical edge cases tested.
- [ ] Recovery tests pass.
- [ ] Static analysis passes.
- [ ] Type checking passes where applicable.
- [ ] No known critical security issues.
- [ ] No secrets committed.

## Documentation

- [ ] README complete.
- [ ] Installation guide complete.
- [ ] Configuration documented.
- [ ] Architecture documented.
- [ ] Electrical limitations documented.
- [ ] API documented.
- [ ] Troubleshooting documented.
- [ ] Test results documented.

## Final verification

- [ ] Real HA read-only validation completed.
- [ ] Actual entity mapping reviewed.
- [ ] No unauthorized physical device actions performed.
- [ ] Final installation artifacts generated.
- [ ] Final project status report generated.

---

# 27. Required Final Deliverables

When the project reaches completion, provide:

1. Complete source repository.
2. Home Assistant Custom Integration.
3. Home Assistant OS App/Add-on.
4. Independent Python Engine.
5. Database schema and migration system.
6. Synthetic household simulator.
7. Automated test suite.
8. Test and accuracy reports.
9. Real HA environment audit.
10. CT update profile.
11. Initial device and CT mapping.
12. Installation documentation.
13. Configuration documentation.
14. Architecture documentation.
15. Troubleshooting documentation.
16. Example configuration.
17. `.env.example`.
18. Secure `.gitignore`.
19. Release notes.
20. Final implementation report.

The final report must clearly identify:

- What is fully implemented.
- What has passed testing.
- What has been validated against real HA data.
- What remains unverified.
- What limitations remain.
- What needs user authorization.
- How to install and start the system.

Never report a feature as implemented unless the code exists and its behavior has been tested.

Never report real-world accuracy unless it has been measured against suitable ground truth.

---

# 28. Immediate Execution Instructions

Start now.

Do not respond with another high-level plan and stop.

Execute the work.

Your first actions must be:

1. Inspect the development environment.
2. Discover the available subagent and parallel task capabilities.
3. Locate `.env` securely.
4. Inspect the existing repository and avoid overwriting user work.
5. Start the Home Assistant environment audit.
6. Start independent architecture, database, algorithm and test-framework workstreams.
7. Create the project task board and status documents.
8. Establish shared data contracts.
9. Begin implementation as soon as dependencies permit.

Use subagents aggressively and intelligently.

Do not spend time optimizing token usage.

Do not wait for the user to provide entity IDs.

Do not stop after producing a design document.

Do not stop after creating project scaffolding.

Do not stop after writing a few example sensors.

**Build the actual working WattMosaic system.**

The final objective is a reliable, installable, continuously running multi-CT intelligent home energy load disaggregation system integrated with the user's real Home Assistant environment.

Your responsibility is to orchestrate the entire engineering process through verified delivery.
