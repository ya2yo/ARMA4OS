use crate::error::ConfigError;
use serde::{Deserialize, Deserializer, Serialize};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum BackendKind {
    #[default]
    Simulated,
    Real,
}

impl<'de> Deserialize<'de> for BackendKind {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let s = String::deserialize(deserializer)?;
        match s.to_lowercase().as_str() {
            "simulated" => Ok(BackendKind::Simulated),
            "real" => Ok(BackendKind::Real),
            other => Err(serde::de::Error::custom(format!(
                "unknown backend '{}': must be 'simulated' or 'real'",
                other
            ))),
        }
    }
}

impl BackendKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            BackendKind::Simulated => "simulated",
            BackendKind::Real => "real",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PolicyConfig {
    pub id: String,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub kernel_ops: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutorConfig {
    pub schema_version: u32,
    #[serde(default)]
    pub backend: BackendKind,
    pub state_dir: PathBuf,
    #[serde(default = "default_sysfs_root")]
    pub sysfs_root: PathBuf,
    #[serde(default = "default_start_timeout_ms")]
    pub start_timeout_ms: u64,
    #[serde(default = "default_stop_timeout_ms")]
    pub stop_timeout_ms: u64,
    #[serde(default = "default_poll_interval_ms")]
    pub poll_interval_ms: u64,
    pub policies: Vec<PolicyConfig>,
}

fn default_sysfs_root() -> PathBuf {
    PathBuf::from("/sys/kernel/sched_ext")
}

fn default_start_timeout_ms() -> u64 {
    5000
}

fn default_stop_timeout_ms() -> u64 {
    3000
}

fn default_poll_interval_ms() -> u64 {
    20
}

impl ExecutorConfig {
    pub fn load_from_file<P: AsRef<Path>>(path: P) -> Result<Self, ConfigError> {
        let content = std::fs::read_to_string(path.as_ref())?;
        Self::from_json_str(&content)
    }

    pub fn from_json_str(s: &str) -> Result<Self, ConfigError> {
        let config: ExecutorConfig = serde_json::from_str(s)?;
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.schema_version != 1 {
            return Err(ConfigError::Validation(format!(
                "unsupported schema_version {}: expected 1",
                self.schema_version
            )));
        }

        if self.state_dir.as_os_str().is_empty() {
            return Err(ConfigError::Validation(
                "state_dir must not be empty".to_string(),
            ));
        }

        if self.start_timeout_ms == 0 {
            return Err(ConfigError::Validation(
                "start_timeout_ms must be greater than 0".to_string(),
            ));
        }

        if self.stop_timeout_ms == 0 {
            return Err(ConfigError::Validation(
                "stop_timeout_ms must be greater than 0".to_string(),
            ));
        }

        if self.poll_interval_ms == 0 {
            return Err(ConfigError::Validation(
                "poll_interval_ms must be greater than 0".to_string(),
            ));
        }

        if self.policies.is_empty() {
            return Err(ConfigError::Validation(
                "policies array must not be empty".to_string(),
            ));
        }

        let mut seen_ids = HashSet::new();
        let mut has_default = false;

        for policy in &self.policies {
            if policy.id.trim().is_empty() {
                return Err(ConfigError::Validation(
                    "policy id cannot be empty".to_string(),
                ));
            }

            if !seen_ids.insert(policy.id.clone()) {
                return Err(ConfigError::Validation(format!(
                    "duplicate policy id '{}' detected",
                    policy.id
                )));
            }

            if policy.id == "default" {
                has_default = true;
                if !policy.command.is_empty() {
                    return Err(ConfigError::Validation(
                        "default policy command must be empty".to_string(),
                    ));
                }
                if policy.kernel_ops.is_some() {
                    return Err(ConfigError::Validation(
                        "default policy kernel_ops must be null".to_string(),
                    ));
                }
            } else {
                if policy.command.is_empty() {
                    return Err(ConfigError::Validation(format!(
                        "non-default policy '{}' must specify a non-empty command",
                        policy.id
                    )));
                }
                if self.backend == BackendKind::Real {
                    match &policy.kernel_ops {
                        Some(ops) if !ops.trim().is_empty() => {}
                        _ => {
                            return Err(ConfigError::Validation(format!(
                                "policy '{}' must specify non-empty kernel_ops in real backend",
                                policy.id
                            )));
                        }
                    }
                }
            }
        }

        if !has_default {
            return Err(ConfigError::Validation(
                "policies array must include a 'default' policy".to_string(),
            ));
        }

        // Backend-specific validation
        if self.backend == BackendKind::Real {
            if !self.sysfs_root.is_absolute() {
                return Err(ConfigError::Validation(format!(
                    "sysfs_root '{:?}' must be an absolute path in real backend",
                    self.sysfs_root
                )));
            }

            for policy in &self.policies {
                if policy.id == "default" {
                    continue;
                }

                let prog = &policy.command[0];
                if !prog.starts_with('/') {
                    return Err(ConfigError::Validation(format!(
                        "policy '{}' command executable '{}' must be an absolute path in real backend",
                        policy.id, prog
                    )));
                }

                // Check forbidden commands (no shell or sudo)
                for arg in &policy.command {
                    let file_name = Path::new(arg)
                        .file_name()
                        .and_then(|s| s.to_str())
                        .unwrap_or(arg.as_str());

                    if file_name == "sudo"
                        || file_name == "sh"
                        || file_name == "bash"
                        || file_name == "zsh"
                        || arg == "-c"
                    {
                        return Err(ConfigError::Validation(format!(
                            "policy '{}' command contains forbidden argument or program '{}' (no shell or sudo allowed)",
                            policy.id, arg
                        )));
                    }
                }
            }
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_valid_simulated_config() {
        let json = r#"{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "/tmp/test-state",
            "policies": [
                {"id": "default", "command": []},
                {"id": "scx_bpfland", "command": ["/usr/bin/scx_bpfland"], "kernel_ops": "bpfland"}
            ]
        }"#;
        let cfg = ExecutorConfig::from_json_str(json).unwrap();
        assert_eq!(cfg.backend, BackendKind::Simulated);
        assert_eq!(cfg.policies.len(), 2);
    }

    #[test]
    fn test_unknown_backend() {
        let json = r#"{
            "schema_version": 1,
            "backend": "quantum",
            "state_dir": "/tmp/test-state",
            "policies": [
                {"id": "default", "command": []}
            ]
        }"#;
        let err = ExecutorConfig::from_json_str(json).unwrap_err();
        match err {
            ConfigError::Json(e) => {
                assert!(e.to_string().contains("unknown backend 'quantum'"));
            }
            _ => panic!("Expected Json deserialization error with unknown backend message"),
        }
    }

    #[test]
    fn test_missing_default_policy() {
        let json = r#"{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "/tmp/test-state",
            "policies": [
                {"id": "scx_bpfland", "command": ["/usr/bin/scx_bpfland"]}
            ]
        }"#;
        let err = ExecutorConfig::from_json_str(json).unwrap_err();
        assert!(matches!(err, ConfigError::Validation(_)));
        assert!(err
            .to_string()
            .contains("policies array must include a 'default' policy"));
    }

    #[test]
    fn test_duplicate_policy_id() {
        let json = r#"{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "/tmp/test-state",
            "policies": [
                {"id": "default", "command": []},
                {"id": "scx_bpfland", "command": ["/usr/bin/scx_bpfland"]},
                {"id": "scx_bpfland", "command": ["/usr/bin/other"]}
            ]
        }"#;
        let err = ExecutorConfig::from_json_str(json).unwrap_err();
        assert!(matches!(err, ConfigError::Validation(_)));
        assert!(err
            .to_string()
            .contains("duplicate policy id 'scx_bpfland'"));
    }

    #[test]
    fn test_real_backend_relative_command() {
        let json = r#"{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "/tmp/test-state",
            "sysfs_root": "/sys/kernel/sched_ext",
            "policies": [
                {"id": "default", "command": []},
                {"id": "scx_bpfland", "command": ["scx_bpfland"], "kernel_ops": "bpfland"}
            ]
        }"#;
        let err = ExecutorConfig::from_json_str(json).unwrap_err();
        assert!(matches!(err, ConfigError::Validation(_)));
        assert!(err.to_string().contains("must be an absolute path"));
    }

    #[test]
    fn test_real_backend_forbidden_shell_sudo() {
        let json = r#"{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "/tmp/test-state",
            "sysfs_root": "/sys/kernel/sched_ext",
            "policies": [
                {"id": "default", "command": []},
                {"id": "scx_bpfland", "command": ["/usr/bin/sudo", "/usr/bin/scx_bpfland"], "kernel_ops": "bpfland"}
            ]
        }"#;
        let err = ExecutorConfig::from_json_str(json).unwrap_err();
        assert!(matches!(err, ConfigError::Validation(_)));
        assert!(err.to_string().contains("no shell or sudo allowed"));
    }

    #[test]
    fn test_default_policy_with_command_or_kernel_ops() {
        let json_cmd = r#"{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "/tmp/test-state",
            "policies": [
                {"id": "default", "command": ["/usr/bin/sleep"]}
            ]
        }"#;
        let err_cmd = ExecutorConfig::from_json_str(json_cmd).unwrap_err();
        assert!(matches!(err_cmd, ConfigError::Validation(_)));
        assert!(err_cmd
            .to_string()
            .contains("default policy command must be empty"));

        let json_ops = r#"{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "/tmp/test-state",
            "policies": [
                {"id": "default", "command": [], "kernel_ops": "something"}
            ]
        }"#;
        let err_ops = ExecutorConfig::from_json_str(json_ops).unwrap_err();
        assert!(matches!(err_ops, ConfigError::Validation(_)));
        assert!(err_ops
            .to_string()
            .contains("default policy kernel_ops must be null"));
    }

    #[test]
    fn test_real_backend_missing_kernel_ops() {
        let json = r#"{
            "schema_version": 1,
            "backend": "real",
            "state_dir": "/tmp/test-state",
            "sysfs_root": "/sys/kernel/sched_ext",
            "policies": [
                {"id": "default", "command": []},
                {"id": "scx_bpfland", "command": ["/usr/bin/scx_bpfland"]}
            ]
        }"#;
        let err = ExecutorConfig::from_json_str(json).unwrap_err();
        assert!(matches!(err, ConfigError::Validation(_)));
        assert!(err
            .to_string()
            .contains("must specify non-empty kernel_ops in real backend"));
    }

    #[test]
    fn test_reject_unknown_fields_in_config() {
        let json = r#"{
            "schema_version": 1,
            "backend": "simulated",
            "state_dir": "/tmp/test-state",
            "unknown_field": "disallowed",
            "policies": [
                {"id": "default", "command": []}
            ]
        }"#;
        let err = ExecutorConfig::from_json_str(json).unwrap_err();
        assert!(matches!(err, ConfigError::Json(_)));
    }
}
