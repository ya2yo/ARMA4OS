use crate::config::PolicyConfig;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProtocolRequest {
    pub schema_version: u32,
    pub request_id: String,
    pub operation: String,
    #[serde(default = "default_request_timeout")]
    pub timeout_ms: u64,
    #[serde(default)]
    pub params: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EmptyParams {}

fn default_request_timeout() -> u64 {
    5000
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProtocolResponse {
    pub schema_version: u32,
    pub request_id: String,
    pub ok: bool,
    pub result: Option<serde_json::Value>,
    pub error: Option<ProtocolError>,
}

impl ProtocolResponse {
    pub fn success(request_id: String, result: serde_json::Value) -> Self {
        Self {
            schema_version: 1,
            request_id,
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    pub fn error<C: Into<String>, M: Into<String>>(
        request_id: String,
        code: C,
        message: M,
    ) -> Self {
        Self {
            schema_version: 1,
            request_id,
            ok: false,
            result: None,
            error: Some(ProtocolError {
                code: code.into(),
                message: message.into(),
            }),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProtocolError {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StatusResult {
    pub policy_id: String,
    pub kernel_state: String,
    pub kernel_ops: Option<String>,
    pub managed_pid: Option<u32>,
    pub healthy: bool,
    pub backend: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ActionResult {
    pub planned_policy: String,
    pub previous_policy: String,
    pub actual_policy: String,
    pub status: String, // "applied" | "held" | "dry_run" | "rolled_back" | "failed"
    pub verified: bool,
    pub started_at_unix_ms: u64,
    pub finished_at_unix_ms: u64,
    pub managed_pid: Option<u32>,
    pub error: Option<String>,
    pub recovery: Option<Box<ActionResult>>,
    pub backend: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CapabilitiesResult {
    pub backend: String,
    pub supported_operations: Vec<String>,
    pub supported_policies: Vec<String>,
    pub kernel_sched_ext_supported: bool,
    pub sysfs_root: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ListResult {
    pub policies: Vec<PolicyConfig>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ApplyParams {
    pub policy_id: String,
    pub expected_policy: String,
    pub expires_at_unix_ms: u64,
    #[serde(default)]
    pub dry_run: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RollbackParams {
    #[serde(default)]
    pub policy_id: Option<String>,
    pub expected_policy: String,
    pub expires_at_unix_ms: u64,
    #[serde(default)]
    pub dry_run: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ShutdownResult {
    pub shutdown: bool,
    pub cleaned_up_pid: Option<u32>,
}
