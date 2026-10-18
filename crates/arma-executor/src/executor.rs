use crate::config::{BackendKind, ExecutorConfig, PolicyConfig};
use crate::error::ExecutorError;
use crate::protocol::{
    ActionResult, ApplyParams, CapabilitiesResult, EmptyParams, ListResult, ProtocolRequest,
    ProtocolResponse, RollbackParams, ShutdownResult, StatusResult,
};
use crate::sysfs::SysfsReader;
use std::collections::{HashMap, VecDeque};
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const MAX_CACHE_SIZE: usize = 10_000;

pub fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

pub struct ManagedChild {
    pub child: Child,
    pub policy_id: String,
    pub expected_ops: Option<String>,
    pub pid: u32,
    pub pgid: libc::pid_t,
    pub started_at_unix_ms: u64,
}

pub struct ArmaExecutor {
    pub config: ExecutorConfig,
    pub sysfs_reader: SysfsReader,
    pub current_policy: String,
    pub last_known_policy: String,
    pub managed_child: Option<ManagedChild>,
    pub request_cache: HashMap<String, (serde_json::Value, ProtocolResponse)>,
    read_cache: HashMap<String, (serde_json::Value, ProtocolResponse)>,
    read_order: VecDeque<String>,
    pub last_child_exit_status: Option<std::process::ExitStatus>,
    pub last_was_shutdown: bool,
}

impl Drop for ArmaExecutor {
    fn drop(&mut self) {
        if self.managed_child.is_some() {
            eprintln!("ArmaExecutor drop: performing best-effort owned child cleanup");
            let _ = self.terminate_child(Some(Duration::from_millis(500)));
        }
    }
}

impl ArmaExecutor {
    pub fn new(config: ExecutorConfig) -> Self {
        let sysfs_reader = SysfsReader::new(&config.sysfs_root);
        Self {
            config,
            sysfs_reader,
            current_policy: "default".to_string(),
            last_known_policy: "default".to_string(),
            managed_child: None,
            request_cache: HashMap::new(),
            read_cache: HashMap::new(),
            read_order: VecDeque::new(),
            last_child_exit_status: None,
            last_was_shutdown: false,
        }
    }

    pub fn is_shut_down(&self) -> bool {
        self.last_was_shutdown
    }

    pub fn has_policy(&self, id: &str) -> bool {
        self.config.policies.iter().any(|p| p.id == id)
    }

    fn owns_detached_child(&self) -> bool {
        self.config.backend == BackendKind::Real
            && self.managed_child.is_some()
            && self
                .sysfs_reader
                .read_state()
                .is_ok_and(|state| state == "disabled")
    }

    pub fn find_policy(&self, id: &str) -> Option<&PolicyConfig> {
        self.config.policies.iter().find(|p| p.id == id)
    }

    pub fn check_child_liveness(&mut self) {
        if let Some(ref mut managed) = self.managed_child {
            match managed.child.try_wait() {
                Ok(Some(status)) => {
                    eprintln!(
                        "Managed child process PID {} for policy '{}' exited with status: {:?}",
                        managed.pid, managed.policy_id, status
                    );
                    self.last_child_exit_status = Some(status);
                    self.managed_child = None;
                }
                Ok(None) => {
                    // Still running normally
                }
                Err(e) => {
                    eprintln!("Failed to check status of child PID {}: {}", managed.pid, e);
                }
            }
        }
    }

    pub fn check_external_conflict(&mut self) -> Result<(), ExecutorError> {
        self.check_child_liveness();
        if self.config.backend == BackendKind::Real {
            let (state, ops) = self.sysfs_reader.read_state_and_ops().map_err(|e| {
                ExecutorError::ExternalConflict(format!("Failed to read sysfs: {}", e))
            })?;

            if state == "unsupported" {
                return Err(ExecutorError::ExternalConflict(
                    "sched_ext is unsupported on this system".to_string(),
                ));
            }

            if state != "disabled" && state != "enabled" {
                return Err(ExecutorError::ExternalConflict(format!(
                    "Kernel scheduler in unknown state '{}'",
                    state
                )));
            }

            if state == "disabled" {
                if let Some(ref child) = self.managed_child {
                    return Err(ExecutorError::ExternalConflict(format!(
                        "Managed child PID {} for policy '{}' is still running, but sysfs state is 'disabled'",
                        child.pid, child.policy_id
                    )));
                }
            } else if state == "enabled" {
                if let Some(ref child) = self.managed_child {
                    let ops_match = match (&child.expected_ops, &ops) {
                        (Some(exp), Some(actual))
                            if !exp.trim().is_empty() && !actual.trim().is_empty() =>
                        {
                            exp == actual
                        }
                        _ => false,
                    };
                    if !ops_match {
                        return Err(ExecutorError::ExternalConflict(format!(
                            "External scheduler active (state='{}', ops='{:?}', owned_ops='{:?}')",
                            state, ops, child.expected_ops
                        )));
                    }
                } else {
                    return Err(ExecutorError::ExternalConflict(format!(
                        "External scheduler active in kernel (state='{}', ops='{:?}') while no owned child process exists",
                        state, ops
                    )));
                }
            }
        }
        Ok(())
    }

    pub fn status(&mut self) -> StatusResult {
        self.check_child_liveness();

        match self.config.backend {
            BackendKind::Simulated => {
                if self.current_policy == "default" {
                    StatusResult {
                        policy_id: "default".to_string(),
                        kernel_state: "disabled".to_string(),
                        kernel_ops: None,
                        managed_pid: None,
                        healthy: true,
                        backend: "simulated".to_string(),
                    }
                } else if let Some(ref child) = self.managed_child {
                    let policy_cfg = self.find_policy(&self.current_policy);
                    let ops = policy_cfg.and_then(|p| p.kernel_ops.clone());
                    StatusResult {
                        policy_id: self.current_policy.clone(),
                        kernel_state: "enabled".to_string(),
                        kernel_ops: ops,
                        managed_pid: Some(child.pid),
                        healthy: true,
                        backend: "simulated".to_string(),
                    }
                } else {
                    // Policy was non-default, but managed child died
                    StatusResult {
                        policy_id: "default".to_string(),
                        kernel_state: "disabled".to_string(),
                        kernel_ops: None,
                        managed_pid: None,
                        healthy: false,
                        backend: "simulated".to_string(),
                    }
                }
            }
            BackendKind::Real => {
                let (state, ops) = match self.sysfs_reader.read_state_and_ops() {
                    Ok(res) => res,
                    Err(e) => {
                        eprintln!("Failed to read sysfs: {}", e);
                        return StatusResult {
                            policy_id: "external".to_string(),
                            kernel_state: "unsupported".to_string(),
                            kernel_ops: None,
                            managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                            healthy: false,
                            backend: "real".to_string(),
                        };
                    }
                };

                if state == "unsupported" {
                    StatusResult {
                        policy_id: "external".to_string(),
                        kernel_state: "unsupported".to_string(),
                        kernel_ops: None,
                        managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                        healthy: false,
                        backend: "real".to_string(),
                    }
                } else if state == "disabled" {
                    if let Some(ref child) = self.managed_child {
                        // Managed child is still alive but sysfs is disabled!
                        StatusResult {
                            policy_id: "external".to_string(),
                            kernel_state: "disabled".to_string(),
                            kernel_ops: None,
                            managed_pid: Some(child.pid),
                            healthy: false,
                            backend: "real".to_string(),
                        }
                    } else {
                        let child_died =
                            self.current_policy != "default" && self.managed_child.is_none();
                        StatusResult {
                            policy_id: "default".to_string(),
                            kernel_state: "disabled".to_string(),
                            kernel_ops: None,
                            managed_pid: None,
                            healthy: !child_died,
                            backend: "real".to_string(),
                        }
                    }
                } else if state == "enabled" {
                    if let Some(ref child) = self.managed_child {
                        let ops_match = match (&child.expected_ops, &ops) {
                            (Some(exp), Some(actual))
                                if !exp.trim().is_empty() && !actual.trim().is_empty() =>
                            {
                                exp == actual
                            }
                            _ => false,
                        };
                        if ops_match {
                            StatusResult {
                                policy_id: child.policy_id.clone(),
                                kernel_state: "enabled".to_string(),
                                kernel_ops: ops,
                                managed_pid: Some(child.pid),
                                healthy: true,
                                backend: "real".to_string(),
                            }
                        } else {
                            StatusResult {
                                policy_id: "external".to_string(),
                                kernel_state: "enabled".to_string(),
                                kernel_ops: ops,
                                managed_pid: Some(child.pid),
                                healthy: false,
                                backend: "real".to_string(),
                            }
                        }
                    } else {
                        // Kernel enabled, but not owned by executor
                        StatusResult {
                            policy_id: "external".to_string(),
                            kernel_state: "enabled".to_string(),
                            kernel_ops: ops,
                            managed_pid: None,
                            healthy: false,
                            backend: "real".to_string(),
                        }
                    }
                } else {
                    StatusResult {
                        policy_id: "external".to_string(),
                        kernel_state: state,
                        kernel_ops: ops,
                        managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                        healthy: false,
                        backend: "real".to_string(),
                    }
                }
            }
        }
    }

    pub fn capabilities(&self) -> CapabilitiesResult {
        let supported_ops = vec![
            "capabilities".to_string(),
            "list".to_string(),
            "status".to_string(),
            "apply".to_string(),
            "rollback".to_string(),
            "shutdown".to_string(),
        ];
        let supported_policies = self.config.policies.iter().map(|p| p.id.clone()).collect();

        let sched_ext_supported = match self.config.backend {
            BackendKind::Simulated => true,
            BackendKind::Real => self.sysfs_reader.is_sched_ext_supported(),
        };

        CapabilitiesResult {
            backend: self.config.backend.as_str().to_string(),
            supported_operations: supported_ops,
            supported_policies,
            kernel_sched_ext_supported: sched_ext_supported,
            sysfs_root: self.config.sysfs_root.to_string_lossy().to_string(),
        }
    }

    pub fn list(&self) -> ListResult {
        ListResult {
            policies: self.config.policies.clone(),
        }
    }

    pub fn terminate_child(&mut self, wait_budget: Option<Duration>) -> Result<(), ExecutorError> {
        if self.managed_child.is_none() {
            return Ok(());
        }

        let managed = self.managed_child.as_mut().unwrap();
        let pid = managed.pid as libc::pid_t;
        let pgid = managed.pgid;

        eprintln!(
            "Sending SIGTERM to managed child PID {} (pgid {})",
            pid, pgid
        );
        unsafe {
            if libc::killpg(pgid, libc::SIGTERM) != 0 {
                libc::kill(pid, libc::SIGTERM);
            }
        }

        let stop_timeout =
            wait_budget.unwrap_or_else(|| Duration::from_millis(self.config.stop_timeout_ms));
        let poll_interval = Duration::from_millis(self.config.poll_interval_ms.clamp(5, 100));
        let start = Instant::now();
        let mut exited = false;

        while start.elapsed() < stop_timeout {
            match managed.child.try_wait() {
                Ok(Some(status)) => {
                    self.last_child_exit_status = Some(status);
                    exited = true;
                    break;
                }
                Ok(None) => {}
                Err(e) => {
                    eprintln!("Error checking managed child PID {}: {}", pid, e);
                }
            }
            thread::sleep(poll_interval);
        }

        if !exited {
            eprintln!(
                "Child PID {} did not terminate in {:?}, sending SIGKILL to pgid {}",
                pid, stop_timeout, pgid
            );
            unsafe {
                if libc::killpg(pgid, libc::SIGKILL) != 0 {
                    libc::kill(pid, libc::SIGKILL);
                }
            }

            // Bounded wait after SIGKILL (never unbounded child.wait()!)
            let kill_wait = Duration::from_millis(1000);
            let kill_start = Instant::now();
            while kill_start.elapsed() < kill_wait {
                match managed.child.try_wait() {
                    Ok(Some(status)) => {
                        self.last_child_exit_status = Some(status);
                        exited = true;
                        break;
                    }
                    Ok(None) => {}
                    Err(e) => {
                        eprintln!(
                            "Error checking managed child PID {} after SIGKILL: {}",
                            pid, e
                        );
                    }
                }
                thread::sleep(poll_interval);
            }
        }

        if !exited {
            // Child could NOT be reaped! DO NOT DROP OWNERSHIP!
            return Err(ExecutorError::TerminationFailed(format!(
                "Managed child PID {} could not be reaped after SIGKILL within bounded timeout",
                pid
            )));
        }

        // Confirmed reaped: safely take ownership
        let _ = self.managed_child.take();

        // Verify kernel state transitions to disabled in real backend
        if self.config.backend == BackendKind::Real {
            let verify_budget =
                wait_budget.unwrap_or_else(|| Duration::from_millis(self.config.stop_timeout_ms));
            let start_v = Instant::now();
            let mut disabled = false;
            while start_v.elapsed() < verify_budget {
                if let Ok(state) = self.sysfs_reader.read_state() {
                    if state == "disabled" {
                        disabled = true;
                        break;
                    }
                }
                thread::sleep(poll_interval);
            }
            if !disabled {
                return Err(ExecutorError::TerminationFailed(
                    "Kernel sched_ext state failed to return to 'disabled' after child termination"
                        .to_string(),
                ));
            }
        }

        Ok(())
    }

    pub fn try_switch_policy(
        &mut self,
        target: &str,
        deadline: Instant,
    ) -> Result<Option<u32>, ExecutorError> {
        let remaining_stop = deadline.saturating_duration_since(Instant::now());
        if remaining_stop.is_zero() {
            return Err(ExecutorError::DeadlineExceeded(
                "Action deadline exceeded before stopping previous policy".to_string(),
            ));
        }

        let stop_budget = std::cmp::min(
            Duration::from_millis(self.config.stop_timeout_ms),
            remaining_stop,
        );

        // 1. Terminate currently managed child
        self.terminate_child(Some(stop_budget))?;

        // 2. If target is default, nothing to spawn
        if target == "default" {
            if self.config.backend == BackendKind::Real {
                let state = self.sysfs_reader.read_state().map_err(|e| {
                    ExecutorError::Internal(format!("Failed to verify sysfs state: {}", e))
                })?;
                if state != "disabled" {
                    return Err(ExecutorError::Internal(format!(
                        "Expected kernel state 'disabled' after stopping child, but got '{}'",
                        state
                    )));
                }
            }
            self.current_policy = "default".to_string();
            return Ok(None);
        }

        let policy_cfg = self
            .find_policy(target)
            .cloned()
            .ok_or_else(|| ExecutorError::PolicyNotFound(target.to_string()))?;

        let remaining_start = deadline.saturating_duration_since(Instant::now());
        if remaining_start.is_zero() {
            return Err(ExecutorError::DeadlineExceeded(
                "Action deadline exceeded before starting new policy".to_string(),
            ));
        }

        match self.config.backend {
            BackendKind::Simulated => {
                // Check if configured to simulate a failure
                if policy_cfg.id.contains("simulate_fail")
                    || policy_cfg.command.first().map(|s| s.as_str()).unwrap_or("")
                        == "simulate_fail"
                {
                    return Err(ExecutorError::ChildExitedPrematurely(
                        "Simulated child process failed to start".to_string(),
                    ));
                }

                if policy_cfg.id.contains("simulate_timeout")
                    || policy_cfg.command.first().map(|s| s.as_str()).unwrap_or("")
                        == "simulate_timeout"
                {
                    return Err(ExecutorError::StartVerificationTimeout(
                        "Simulated start verification timed out".to_string(),
                    ));
                }

                // In simulated mode, spawn a safe dummy process (sleep 86400) to represent managed process
                let mut cmd = Command::new("sleep");
                cmd.arg("86400")
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());

                unsafe {
                    cmd.pre_exec(|| {
                        let _ = libc::setpgid(0, 0);
                        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
                        Ok(())
                    });
                }

                let child = cmd.spawn().map_err(|e| {
                    ExecutorError::Internal(format!(
                        "Failed to spawn simulated dummy process: {}",
                        e
                    ))
                })?;

                let pid = child.id();
                let pgid = pid as libc::pid_t;
                self.managed_child = Some(ManagedChild {
                    child,
                    policy_id: target.to_string(),
                    expected_ops: policy_cfg.kernel_ops.clone(),
                    pid,
                    pgid,
                    started_at_unix_ms: now_unix_ms(),
                });

                self.current_policy = target.to_string();
                Ok(Some(pid))
            }
            BackendKind::Real => {
                if policy_cfg.command.is_empty() {
                    return Err(ExecutorError::Config(
                        crate::error::ConfigError::Validation(format!(
                            "Policy '{}' has empty command",
                            target
                        )),
                    ));
                }

                let prog = &policy_cfg.command[0];
                let args = &policy_cfg.command[1..];

                // Direct argv execution, absolute path, no shell, no sudo
                let mut cmd = Command::new(prog);
                cmd.args(args);
                cmd.stdin(Stdio::null());
                cmd.stdout(Stdio::null());
                cmd.stderr(Stdio::inherit());

                // Terminate child if parent dies and put child into own process group
                unsafe {
                    cmd.pre_exec(|| {
                        if libc::setpgid(0, 0) != 0 {
                            return Err(std::io::Error::last_os_error());
                        }
                        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
                        Ok(())
                    });
                }

                let mut child = cmd.spawn().map_err(|e| {
                    ExecutorError::Internal(format!(
                        "Failed to spawn policy '{}' ({}): {}",
                        target, prog, e
                    ))
                })?;

                let pid = child.id();
                let pgid = pid as libc::pid_t;

                let max_start_wait = std::cmp::min(
                    Duration::from_millis(self.config.start_timeout_ms),
                    remaining_start,
                );
                let poll_interval =
                    Duration::from_millis(self.config.poll_interval_ms.clamp(5, 100));
                let start_time = Instant::now();
                let mut verified = false;

                while start_time.elapsed() < max_start_wait {
                    // Check child death
                    if let Ok(Some(exit_status)) = child.try_wait() {
                        return Err(ExecutorError::ChildExitedPrematurely(format!(
                            "Child PID {} exited prematurely with status {:?}",
                            pid, exit_status
                        )));
                    }

                    // Check sysfs state & ops
                    if let Ok((state, ops)) = self.sysfs_reader.read_state_and_ops() {
                        if state == "enabled" {
                            let ops_match = match (&policy_cfg.kernel_ops, &ops) {
                                (Some(exp), Some(actual))
                                    if !exp.trim().is_empty() && !actual.trim().is_empty() =>
                                {
                                    exp == actual
                                }
                                _ => false,
                            };
                            if ops_match {
                                verified = true;
                                break;
                            }
                        }
                    }

                    thread::sleep(poll_interval);
                }

                if !verified {
                    eprintln!(
                        "Start verification timed out for policy '{}', killing child PID {} (pgid {})",
                        target, pid, pgid
                    );
                    unsafe {
                        if libc::killpg(pgid, libc::SIGTERM) != 0 {
                            libc::kill(pid as libc::pid_t, libc::SIGTERM);
                        }
                    }

                    // Bounded wait after SIGTERM
                    let mut reaped = false;
                    let k_start = Instant::now();
                    while k_start.elapsed() < Duration::from_millis(500) {
                        if let Ok(Some(_)) = child.try_wait() {
                            reaped = true;
                            break;
                        }
                        thread::sleep(poll_interval);
                    }

                    if !reaped {
                        unsafe {
                            if libc::killpg(pgid, libc::SIGKILL) != 0 {
                                libc::kill(pid as libc::pid_t, libc::SIGKILL);
                            }
                        }
                        let k2_start = Instant::now();
                        while k2_start.elapsed() < Duration::from_millis(1000) {
                            if let Ok(Some(_)) = child.try_wait() {
                                break;
                            }
                            thread::sleep(poll_interval);
                        }
                    }

                    if start_time.elapsed() >= remaining_start {
                        return Err(ExecutorError::DeadlineExceeded(format!(
                            "Action deadline budget exceeded during start verification of policy '{}'",
                            target
                        )));
                    } else {
                        return Err(ExecutorError::StartVerificationTimeout(format!(
                            "Timed out after {:?} waiting for sysfs state='enabled' and ops='{:?}'",
                            max_start_wait, policy_cfg.kernel_ops
                        )));
                    }
                }

                self.managed_child = Some(ManagedChild {
                    child,
                    policy_id: target.to_string(),
                    expected_ops: policy_cfg.kernel_ops.clone(),
                    pid,
                    pgid,
                    started_at_unix_ms: now_unix_ms(),
                });

                self.current_policy = target.to_string();
                Ok(Some(pid))
            }
        }
    }

    fn recover_to_target_or_default(
        &mut self,
        target_policy: &str,
        failed_policy: &str,
        original_err: &str,
    ) -> ActionResult {
        let rec_started = now_unix_ms();
        let backend_str = self.config.backend.as_str().to_string();

        // Emergency recovery phase is independently bounded:
        // It is granted an emergency budget (stop_timeout_ms + start_timeout_ms)
        // independent of the original request's exhausted deadline to ensure the system is left in a safe state.
        let emergency_budget =
            Duration::from_millis(self.config.stop_timeout_ms + self.config.start_timeout_ms);
        let emergency_deadline = Instant::now() + emergency_budget;

        eprintln!(
            "Attempting emergency recovery rollback to policy '{}' (independent budget: {:?})...",
            target_policy, emergency_budget
        );

        match self.try_switch_policy(target_policy, emergency_deadline) {
            Ok(pid) => {
                self.current_policy = target_policy.to_string();
                ActionResult {
                    planned_policy: target_policy.to_string(),
                    previous_policy: failed_policy.to_string(),
                    actual_policy: target_policy.to_string(),
                    status: "rolled_back".to_string(),
                    verified: true,
                    started_at_unix_ms: rec_started,
                    finished_at_unix_ms: now_unix_ms(),
                    managed_pid: pid,
                    error: Some(format!(
                        "{}. Emergency recovery phase executed (independent budget {:?}, may extend request deadline). Recovery to '{}' succeeded.",
                        original_err, emergency_budget, target_policy
                    )),
                    recovery: None,
                    backend: backend_str,
                }
            }
            Err(e) => {
                let rollback_err = e.to_string();
                eprintln!(
                    "Rollback to policy '{}' failed: {}. Falling back to default...",
                    target_policy, rollback_err
                );
                let fb_started = now_unix_ms();
                let fb_budget = Duration::from_millis(
                    self.config.stop_timeout_ms + self.config.start_timeout_ms,
                );
                let fb_deadline = Instant::now() + fb_budget;
                match self.try_switch_policy("default", fb_deadline) {
                    Ok(pid) => {
                        self.current_policy = "default".to_string();
                        ActionResult {
                            planned_policy: "default".to_string(),
                            previous_policy: target_policy.to_string(),
                            actual_policy: "default".to_string(),
                            status: "rolled_back".to_string(),
                            verified: true,
                            started_at_unix_ms: fb_started,
                            finished_at_unix_ms: now_unix_ms(),
                            managed_pid: pid,
                            error: Some(format!(
                                "{}. Rollback to '{}' failed ({}), emergency fallback to default succeeded.",
                                original_err, target_policy, rollback_err
                            )),
                            recovery: None,
                            backend: backend_str,
                        }
                    }
                    Err(fb_err) => {
                        self.current_policy = "external".to_string();
                        ActionResult {
                            planned_policy: "default".to_string(),
                            previous_policy: target_policy.to_string(),
                            actual_policy: "external".to_string(),
                            status: "failed".to_string(),
                            verified: false,
                            started_at_unix_ms: fb_started,
                            finished_at_unix_ms: now_unix_ms(),
                            managed_pid: None,
                            error: Some(format!(
                                "{}. Rollback failed ({}) and fallback default also failed ({}). System left in unverified external state.",
                                original_err, rollback_err, fb_err
                            )),
                            recovery: None,
                            backend: backend_str,
                        }
                    }
                }
            }
        }
    }

    pub fn handle_apply(
        &mut self,
        request_id: String,
        params: ApplyParams,
        timeout_ms: u64,
    ) -> ProtocolResponse {
        let now = now_unix_ms();

        // 1. Expiry check (reject <= now)
        if params.expires_at_unix_ms <= now {
            return ProtocolResponse::error(
                request_id,
                "DEADLINE_EXCEEDED",
                format!(
                    "Request expiry {} has passed (now: {})",
                    params.expires_at_unix_ms, now
                ),
            );
        }

        // 2. Policy whitelist check
        if !self.has_policy(&params.policy_id) {
            return ProtocolResponse::error(
                request_id,
                "POLICY_NOT_FOUND",
                format!(
                    "Policy '{}' not found in configuration whitelist",
                    params.policy_id
                ),
            );
        }

        // An owned loader detached by the kernel can always be stopped back to
        // default. This exception never permits loading over an external policy.
        let owned_reset =
            params.policy_id == "default" && !params.dry_run && self.owns_detached_child();
        // 3. External conflict check (fail-closed)
        if let Err(e) = if owned_reset {
            Ok(())
        } else {
            self.check_external_conflict()
        } {
            return ProtocolResponse::error(
                request_id,
                "EXTERNAL_SCHEDULER_CONFLICT",
                e.to_string(),
            );
        }

        // 4. Verify expected_policy against fresh status().policy_id
        let cur_status = self.status();
        if params.expected_policy != cur_status.policy_id {
            return ProtocolResponse::error(
                request_id,
                "STATE_CONFLICT",
                format!(
                    "Expected policy '{}' does not match fresh kernel status policy '{}'",
                    params.expected_policy, cur_status.policy_id
                ),
            );
        }

        // 5. Refuse dry_run and held 'verified:true' when kernel state unsupported, unknown, or managed child still alive but sysfs disabled
        let kernel_state_unsupported = cur_status.kernel_state == "unsupported";
        let kernel_state_unknown = cur_status.kernel_state != "enabled"
            && cur_status.kernel_state != "disabled"
            && cur_status.kernel_state != "unsupported";
        let child_alive_sysfs_disabled =
            self.managed_child.is_some() && cur_status.kernel_state == "disabled";
        let is_external = cur_status.policy_id == "external";

        if kernel_state_unsupported
            || kernel_state_unknown
            || ((child_alive_sysfs_disabled || is_external) && !owned_reset)
        {
            return ProtocolResponse::error(
                request_id,
                "STATE_CONFLICT",
                format!(
                    "Kernel scheduling state is invalid or unverified (state='{}', policy='{}', child_alive_sysfs_disabled={})",
                    cur_status.kernel_state, cur_status.policy_id, child_alive_sysfs_disabled
                ),
            );
        }

        // If cur_status is unhealthy (e.g. child died prematurely):
        // Allow reset if target is default and expected is default. Otherwise reject!
        if !owned_reset
            && !cur_status.healthy
            && (params.policy_id != "default" || params.expected_policy != "default")
        {
            return ProtocolResponse::error(
                request_id,
                "STATE_CONFLICT",
                "Current scheduler state is unhealthy; must reset to default first".to_string(),
            );
        }

        let backend_str = self.config.backend.as_str().to_string();

        // 6. Dry run check
        if params.dry_run {
            let res = ActionResult {
                planned_policy: params.policy_id,
                previous_policy: cur_status.policy_id.clone(),
                actual_policy: cur_status.policy_id,
                status: "dry_run".to_string(),
                verified: true,
                started_at_unix_ms: now,
                finished_at_unix_ms: now_unix_ms(),
                managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                error: None,
                recovery: None,
                backend: backend_str,
            };
            return ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap());
        }

        // 7. Held check
        if params.policy_id == cur_status.policy_id && cur_status.healthy {
            let res = ActionResult {
                planned_policy: params.policy_id.clone(),
                previous_policy: cur_status.policy_id.clone(),
                actual_policy: cur_status.policy_id,
                status: "held".to_string(),
                verified: true,
                started_at_unix_ms: now,
                finished_at_unix_ms: now_unix_ms(),
                managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                error: None,
                recovery: None,
                backend: backend_str,
            };
            return ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap());
        }

        // 8. Monotonic action budget
        let action_deadline = Instant::now() + Duration::from_millis(timeout_ms);
        let prev = cur_status.policy_id;

        match self.try_switch_policy(&params.policy_id, action_deadline) {
            Ok(pid) => {
                self.last_known_policy = if self.has_policy(&prev) {
                    prev.clone()
                } else {
                    "default".to_string()
                };
                self.current_policy = params.policy_id.clone();
                let res = ActionResult {
                    planned_policy: params.policy_id.clone(),
                    previous_policy: prev,
                    actual_policy: params.policy_id,
                    status: "applied".to_string(),
                    verified: true,
                    started_at_unix_ms: now,
                    finished_at_unix_ms: now_unix_ms(),
                    managed_pid: pid,
                    error: None,
                    recovery: None,
                    backend: backend_str,
                };
                ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap())
            }
            Err(err) => {
                let err_msg = err.to_string();
                eprintln!(
                    "Apply of policy '{}' failed: {}. Initiating emergency recovery...",
                    params.policy_id, err_msg
                );

                let recovery_res =
                    self.recover_to_target_or_default(&prev, &params.policy_id, &err_msg);
                let res = ActionResult {
                    planned_policy: params.policy_id,
                    previous_policy: prev,
                    actual_policy: self.current_policy.clone(),
                    status: "failed".to_string(),
                    verified: false,
                    started_at_unix_ms: now,
                    finished_at_unix_ms: now_unix_ms(),
                    managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                    error: Some(err_msg),
                    recovery: Some(Box::new(recovery_res)),
                    backend: backend_str,
                };
                ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap())
            }
        }
    }

    pub fn handle_rollback(
        &mut self,
        request_id: String,
        params: RollbackParams,
        timeout_ms: u64,
    ) -> ProtocolResponse {
        let now = now_unix_ms();

        // 1. Expiry check
        if params.expires_at_unix_ms <= now {
            return ProtocolResponse::error(
                request_id,
                "DEADLINE_EXCEEDED",
                format!(
                    "Request expiry {} has passed (now: {})",
                    params.expires_at_unix_ms, now
                ),
            );
        }

        // 2. Verify expected_policy against fresh status().policy_id
        let cur_status = self.status();
        if params.expected_policy != cur_status.policy_id {
            return ProtocolResponse::error(
                request_id,
                "STATE_CONFLICT",
                format!(
                    "Expected policy '{}' does not match fresh kernel status policy '{}'",
                    params.expected_policy, cur_status.policy_id
                ),
            );
        }

        let target = params
            .policy_id
            .unwrap_or_else(|| self.last_known_policy.clone());

        if !self.has_policy(&target) {
            return ProtocolResponse::error(
                request_id,
                "POLICY_NOT_FOUND",
                format!("Policy '{}' not found in configuration whitelist", target),
            );
        }

        let owned_reset = target == "default" && !params.dry_run && self.owns_detached_child();
        // 3. External conflict check
        if let Err(e) = if owned_reset {
            Ok(())
        } else {
            self.check_external_conflict()
        } {
            return ProtocolResponse::error(
                request_id,
                "EXTERNAL_SCHEDULER_CONFLICT",
                e.to_string(),
            );
        }

        // 4. Verify expected_policy against fresh status().policy_id
        let cur_status = self.status();
        if params.expected_policy != cur_status.policy_id {
            return ProtocolResponse::error(
                request_id,
                "STATE_CONFLICT",
                format!(
                    "Expected policy '{}' does not match fresh kernel status policy '{}'",
                    params.expected_policy, cur_status.policy_id
                ),
            );
        }

        // 5. Kernel state check
        let kernel_state_unsupported = cur_status.kernel_state == "unsupported";
        let kernel_state_unknown = cur_status.kernel_state != "enabled"
            && cur_status.kernel_state != "disabled"
            && cur_status.kernel_state != "unsupported";
        let child_alive_sysfs_disabled =
            self.managed_child.is_some() && cur_status.kernel_state == "disabled";
        let is_external = cur_status.policy_id == "external";

        if kernel_state_unsupported
            || kernel_state_unknown
            || ((child_alive_sysfs_disabled || is_external) && !owned_reset)
        {
            return ProtocolResponse::error(
                request_id,
                "STATE_CONFLICT",
                format!(
                    "Kernel scheduling state is invalid or unverified (state='{}', policy='{}', child_alive_sysfs_disabled={})",
                    cur_status.kernel_state, cur_status.policy_id, child_alive_sysfs_disabled
                ),
            );
        }

        if !owned_reset
            && !cur_status.healthy
            && (target != "default" || params.expected_policy != "default")
        {
            return ProtocolResponse::error(
                request_id,
                "STATE_CONFLICT",
                "Current scheduler state is unhealthy; must reset to default first".to_string(),
            );
        }

        let backend_str = self.config.backend.as_str().to_string();

        if params.dry_run {
            let res = ActionResult {
                planned_policy: target,
                previous_policy: cur_status.policy_id.clone(),
                actual_policy: cur_status.policy_id,
                status: "dry_run".to_string(),
                verified: true,
                started_at_unix_ms: now,
                finished_at_unix_ms: now_unix_ms(),
                managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                error: None,
                recovery: None,
                backend: backend_str,
            };
            return ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap());
        }

        if target == cur_status.policy_id && cur_status.healthy {
            let res = ActionResult {
                planned_policy: target.clone(),
                previous_policy: cur_status.policy_id.clone(),
                actual_policy: target,
                status: "held".to_string(),
                verified: true,
                started_at_unix_ms: now,
                finished_at_unix_ms: now_unix_ms(),
                managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                error: None,
                recovery: None,
                backend: backend_str,
            };
            return ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap());
        }

        let action_deadline = Instant::now() + Duration::from_millis(timeout_ms);
        let prev = cur_status.policy_id;

        match self.try_switch_policy(&target, action_deadline) {
            Ok(pid) => {
                // Repeated rollback keeps its recovery target; it must not
                // reactivate the policy which was just rolled back.
                self.last_known_policy = target.clone();
                self.current_policy = target.clone();
                let res = ActionResult {
                    planned_policy: target.clone(),
                    previous_policy: prev,
                    actual_policy: target,
                    status: "rolled_back".to_string(),
                    verified: true,
                    started_at_unix_ms: now,
                    finished_at_unix_ms: now_unix_ms(),
                    managed_pid: pid,
                    error: None,
                    recovery: None,
                    backend: backend_str,
                };
                ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap())
            }
            Err(err) => {
                let err_msg = err.to_string();
                let recovery_res = self.recover_to_target_or_default("default", &target, &err_msg);
                let res = ActionResult {
                    planned_policy: target,
                    previous_policy: prev,
                    actual_policy: self.current_policy.clone(),
                    status: "failed".to_string(),
                    verified: false,
                    started_at_unix_ms: now,
                    finished_at_unix_ms: now_unix_ms(),
                    managed_pid: self.managed_child.as_ref().map(|c| c.pid),
                    error: Some(err_msg),
                    recovery: Some(Box::new(recovery_res)),
                    backend: backend_str,
                };
                ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap())
            }
        }
    }

    pub fn handle_shutdown(&mut self, request_id: String, timeout_ms: u64) -> ProtocolResponse {
        self.last_was_shutdown = true;
        let cleaned_up_pid = self.managed_child.as_ref().map(|c| c.pid);
        let budget = Duration::from_millis(timeout_ms.min(self.config.stop_timeout_ms));

        match self.terminate_child(Some(budget)) {
            Ok(()) => {
                self.current_policy = "default".to_string();
                let res = ShutdownResult {
                    shutdown: true,
                    cleaned_up_pid,
                };
                ProtocolResponse::success(request_id, serde_json::to_value(res).unwrap())
            }
            Err(e) => {
                eprintln!("Shutdown cleanup failed: {}", e);
                ProtocolResponse::error(
                    request_id,
                    "CLEANUP_FAILED",
                    format!("Shutdown cleanup failed: {}", e),
                )
            }
        }
    }

    pub fn clean_up(&mut self) {
        if let Some(ref child) = self.managed_child {
            eprintln!(
                "Cleaning up managed child PID {} (policy '{}')",
                child.pid, child.policy_id
            );
        }
        match self.terminate_child(None) {
            Ok(()) => {
                self.current_policy = "default".to_string();
            }
            Err(e) => {
                eprintln!("Warning: cleanup failed: {}", e);
            }
        }
    }

    pub fn process_raw_request(&mut self, raw_line: &str) -> ProtocolResponse {
        let trimmed = raw_line.trim();
        if trimmed.is_empty() {
            return ProtocolResponse::error(
                "unknown".to_string(),
                "INVALID_REQUEST",
                "Empty request line",
            );
        }

        let raw_val: serde_json::Value = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(e) => {
                return ProtocolResponse::error(
                    "unknown".to_string(),
                    "INVALID_REQUEST",
                    format!("Failed to parse request JSON: {}", e),
                );
            }
        };

        let request_id = match raw_val.get("request_id").and_then(|v| v.as_str()) {
            Some(id) if !id.trim().is_empty() => id.to_string(),
            _ => {
                return ProtocolResponse::error(
                    "unknown".to_string(),
                    "INVALID_REQUEST",
                    "Missing or invalid 'request_id'",
                );
            }
        };

        // Cache deduplication check with identical body validation
        if let Some((cached_val, cached_resp)) = self
            .request_cache
            .get(&request_id)
            .or_else(|| self.read_cache.get(&request_id))
        {
            if &raw_val == cached_val {
                eprintln!(
                    "Duplicate request_id '{}' with identical body received, returning cached response",
                    request_id
                );
                return cached_resp.clone();
            } else {
                eprintln!(
                    "Duplicate request_id '{}' received with mismatched payload",
                    request_id
                );
                return ProtocolResponse::error(
                    request_id,
                    "DUPLICATE_REQUEST_MISMATCH",
                    "Request ID already processed with a different request payload",
                );
            }
        }

        let req: ProtocolRequest = match serde_json::from_value(raw_val.clone()) {
            Ok(r) => r,
            Err(e) => {
                let resp = ProtocolResponse::error(
                    request_id.clone(),
                    "INVALID_REQUEST",
                    format!("Invalid request structure: {}", e),
                );
                if self.request_cache.len() < MAX_CACHE_SIZE {
                    self.request_cache
                        .insert(request_id, (raw_val, resp.clone()));
                }
                return resp;
            }
        };

        if req.schema_version != 1 {
            let resp = ProtocolResponse::error(
                request_id.clone(),
                "INVALID_REQUEST",
                format!("Unsupported schema_version: {}", req.schema_version),
            );
            if self.request_cache.len() < MAX_CACHE_SIZE {
                self.request_cache
                    .insert(request_id, (raw_val, resp.clone()));
            }
            return resp;
        }

        // Validate timeout_ms in 1..=120000
        if !(1..=120_000).contains(&req.timeout_ms) {
            let resp = ProtocolResponse::error(
                request_id.clone(),
                "INVALID_REQUEST",
                format!(
                    "timeout_ms must be between 1 and 120000, got {}",
                    req.timeout_ms
                ),
            );
            if self.request_cache.len() < MAX_CACHE_SIZE {
                self.request_cache
                    .insert(request_id, (raw_val, resp.clone()));
            }
            return resp;
        }

        let is_mutating = req.operation == "apply" || req.operation == "rollback";

        // Bound cache capacity: refuse new mutating requests after capacity, never evict mutation IDs
        if is_mutating && self.request_cache.len() >= MAX_CACHE_SIZE {
            return ProtocolResponse::error(
                request_id,
                "CAPACITY_EXCEEDED",
                format!(
                    "Request cache capacity reached ({}); new mutating requests refused to prevent replay",
                    MAX_CACHE_SIZE
                ),
            );
        }

        let resp = match req.operation.as_str() {
            "capabilities" => {
                if let Err(e) = serde_json::from_value::<EmptyParams>(req.params) {
                    ProtocolResponse::error(
                        req.request_id,
                        "INVALID_REQUEST",
                        format!("Invalid params for capabilities: {}", e),
                    )
                } else {
                    let res = self.capabilities();
                    ProtocolResponse::success(req.request_id, serde_json::to_value(res).unwrap())
                }
            }
            "list" => {
                if let Err(e) = serde_json::from_value::<EmptyParams>(req.params) {
                    ProtocolResponse::error(
                        req.request_id,
                        "INVALID_REQUEST",
                        format!("Invalid params for list: {}", e),
                    )
                } else {
                    let res = self.list();
                    ProtocolResponse::success(req.request_id, serde_json::to_value(res).unwrap())
                }
            }
            "status" => {
                if let Err(e) = serde_json::from_value::<EmptyParams>(req.params) {
                    ProtocolResponse::error(
                        req.request_id,
                        "INVALID_REQUEST",
                        format!("Invalid params for status: {}", e),
                    )
                } else {
                    let res = self.status();
                    ProtocolResponse::success(req.request_id, serde_json::to_value(res).unwrap())
                }
            }
            "apply" => {
                let params: ApplyParams = match serde_json::from_value(req.params) {
                    Ok(p) => p,
                    Err(e) => {
                        let resp = ProtocolResponse::error(
                            req.request_id.clone(),
                            "INVALID_REQUEST",
                            format!("Invalid apply params: {}", e),
                        );
                        if self.request_cache.len() < MAX_CACHE_SIZE {
                            self.request_cache
                                .insert(request_id, (raw_val, resp.clone()));
                        }
                        return resp;
                    }
                };
                self.handle_apply(req.request_id, params, req.timeout_ms)
            }
            "rollback" => {
                let params: RollbackParams = match serde_json::from_value(req.params) {
                    Ok(p) => p,
                    Err(e) => {
                        let resp = ProtocolResponse::error(
                            req.request_id.clone(),
                            "INVALID_REQUEST",
                            format!("Invalid rollback params: {}", e),
                        );
                        if self.request_cache.len() < MAX_CACHE_SIZE {
                            self.request_cache
                                .insert(request_id, (raw_val, resp.clone()));
                        }
                        return resp;
                    }
                };
                self.handle_rollback(req.request_id, params, req.timeout_ms)
            }
            "shutdown" => {
                if let Err(e) = serde_json::from_value::<EmptyParams>(req.params) {
                    ProtocolResponse::error(
                        req.request_id,
                        "INVALID_REQUEST",
                        format!("Invalid params for shutdown: {}", e),
                    )
                } else {
                    self.handle_shutdown(req.request_id, req.timeout_ms)
                }
            }
            unknown => ProtocolResponse::error(
                req.request_id,
                "INVALID_REQUEST",
                format!("Unknown operation '{}'", unknown),
            ),
        };

        // Monitoring must not consume mutation replay protection during long runs.
        if is_mutating {
            if self.request_cache.len() < MAX_CACHE_SIZE {
                self.request_cache
                    .insert(request_id, (raw_val, resp.clone()));
            }
        } else {
            if self.read_cache.len() >= 1024 {
                if let Some(old) = self.read_order.pop_front() {
                    self.read_cache.remove(&old);
                }
            }
            self.read_order.push_back(request_id.clone());
            self.read_cache.insert(request_id, (raw_val, resp.clone()));
        }
        resp
    }
}
