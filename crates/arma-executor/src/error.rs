use std::fmt;

#[derive(Debug)]
pub enum ConfigError {
    Io(std::io::Error),
    Json(serde_json::Error),
    Validation(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::Io(e) => write!(f, "Configuration I/O error: {}", e),
            ConfigError::Json(e) => write!(f, "Configuration JSON parsing error: {}", e),
            ConfigError::Validation(msg) => write!(f, "Configuration validation error: {}", msg),
        }
    }
}

impl std::error::Error for ConfigError {}

impl From<std::io::Error> for ConfigError {
    fn from(e: std::io::Error) -> Self {
        ConfigError::Io(e)
    }
}

impl From<serde_json::Error> for ConfigError {
    fn from(e: serde_json::Error) -> Self {
        ConfigError::Json(e)
    }
}

#[derive(Debug)]
pub enum LockError {
    Io(String),
    AlreadyLocked(String),
}

impl fmt::Display for LockError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LockError::Io(msg) => write!(f, "Lock I/O error: {}", msg),
            LockError::AlreadyLocked(msg) => write!(f, "State directory already locked: {}", msg),
        }
    }
}

impl std::error::Error for LockError {}

#[derive(Debug)]
pub enum ExecutorError {
    Lock(LockError),
    Config(ConfigError),
    StateConflict(String),
    DeadlineExceeded(String),
    PolicyNotFound(String),
    ExternalConflict(String),
    StartVerificationTimeout(String),
    ChildExitedPrematurely(String),
    TerminationFailed(String),
    DuplicateRequestMismatch(String),
    CapacityExceeded(String),
    InputLineTooLong(String),
    Io(std::io::Error),
    Internal(String),
}

impl fmt::Display for ExecutorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ExecutorError::Lock(e) => write!(f, "Lock error: {}", e),
            ExecutorError::Config(e) => write!(f, "Config error: {}", e),
            ExecutorError::StateConflict(msg) => write!(f, "State conflict: {}", msg),
            ExecutorError::DeadlineExceeded(msg) => write!(f, "Deadline exceeded: {}", msg),
            ExecutorError::PolicyNotFound(msg) => write!(f, "Policy not found: {}", msg),
            ExecutorError::ExternalConflict(msg) => write!(f, "External conflict: {}", msg),
            ExecutorError::StartVerificationTimeout(msg) => {
                write!(f, "Start verification timeout: {}", msg)
            }
            ExecutorError::ChildExitedPrematurely(msg) => {
                write!(f, "Child exited prematurely: {}", msg)
            }
            ExecutorError::TerminationFailed(msg) => write!(f, "Termination failed: {}", msg),
            ExecutorError::DuplicateRequestMismatch(msg) => {
                write!(f, "Duplicate request mismatch: {}", msg)
            }
            ExecutorError::CapacityExceeded(msg) => write!(f, "Capacity exceeded: {}", msg),
            ExecutorError::InputLineTooLong(msg) => write!(f, "Input line too long: {}", msg),
            ExecutorError::Io(e) => write!(f, "I/O error: {}", e),
            ExecutorError::Internal(msg) => write!(f, "Internal error: {}", msg),
        }
    }
}

impl std::error::Error for ExecutorError {}

impl From<LockError> for ExecutorError {
    fn from(e: LockError) -> Self {
        ExecutorError::Lock(e)
    }
}

impl From<ConfigError> for ExecutorError {
    fn from(e: ConfigError) -> Self {
        ExecutorError::Config(e)
    }
}

impl From<std::io::Error> for ExecutorError {
    fn from(e: std::io::Error) -> Self {
        ExecutorError::Io(e)
    }
}
