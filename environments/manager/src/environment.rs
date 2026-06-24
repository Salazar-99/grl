use std::sync::Arc;

use tonic::{Request, Response, Status};

use crate::catalog::Catalog;
use crate::pb::environment_service_server::EnvironmentService;
use crate::pb::{
    CreateEnvironmentRequest, CreateEnvironmentResponse, EvaluateRequest, EvaluateResponse,
    ExecuteRequest, ExecuteResponse, ListTasksRequest, ListTasksResponse, TaskIndexEntry,
    TeardownRequest, TeardownResponse,
};
use crate::registry::{Registry, RegistryError, SUBMIT_TOOL};
use crate::vm;

#[derive(Debug)]
pub struct EnvironmentServiceImpl {
    catalog: Arc<Catalog>,
    env_name: String,
    manager_addr: String,
    registry: Arc<Registry>,
}

impl EnvironmentServiceImpl {
    pub fn new(catalog: Arc<Catalog>) -> Self {
        let env_name = std::env::var("GRL_ENV_ID").unwrap_or_default();
        let manager_addr = std::env::var("GRL_MANAGER_ADVERTISE_ADDR").unwrap_or_default();
        Self {
            catalog,
            env_name,
            manager_addr,
            registry: Arc::new(Registry::from_env()),
        }
    }

    /// Test-only constructor with explicit registry and advertise address.
    #[cfg(test)]
    pub fn with_registry(
        catalog: Arc<Catalog>,
        registry: Arc<Registry>,
        manager_addr: impl Into<String>,
    ) -> Self {
        Self {
            catalog,
            env_name: String::new(),
            manager_addr: manager_addr.into(),
            registry,
        }
    }

    fn registry_status(err: RegistryError) -> Status {
        match err {
            RegistryError::Exhausted => Status::resource_exhausted(err.to_string()),
            RegistryError::NotFound(id) => Status::not_found(id),
            RegistryError::NotReady { .. } => Status::unavailable(err.to_string()),
            RegistryError::AlreadySubmitted { .. } | RegistryError::ExecuteForbidden { .. } => {
                Status::failed_precondition(err.to_string())
            }
            RegistryError::AlreadyEvaluated { .. } => Status::failed_precondition(err.to_string()),
        }
    }

    fn spawn_boot_task(registry: Arc<Registry>, env_id: String, spec: crate::catalog::TaskSpec) {
        tokio::spawn(async move {
            if !vm::boot_enabled() {
                let _ = registry.set_ready(&env_id).await;
                return;
            }
            match vm::boot(&env_id, &spec).await {
                Ok(handle) => {
                    registry.attach_vm(&env_id, handle).await;
                    let _ = registry.set_ready(&env_id).await;
                }
                Err(err) => {
                    eprintln!("VM boot failed for {env_id}: {err}");
                }
            }
        });
    }
}

#[tonic::async_trait]
impl EnvironmentService for EnvironmentServiceImpl {
    async fn list_tasks(
        &self,
        request: Request<ListTasksRequest>,
    ) -> Result<Response<ListTasksResponse>, Status> {
        let split = request.into_inner().split;
        let split_filter = if split.is_empty() {
            None
        } else {
            Some(split.as_str())
        };
        let tasks = self
            .catalog
            .list_tasks(split_filter)
            .into_iter()
            .map(|(task_id, split)| TaskIndexEntry { task_id, split })
            .collect();
        Ok(Response::new(ListTasksResponse {
            tasks,
            env_name: self.env_name.clone(),
        }))
    }

    async fn create_environment(
        &self,
        request: Request<CreateEnvironmentRequest>,
    ) -> Result<Response<CreateEnvironmentResponse>, Status> {
        let task_id = request.into_inner().task_id;
        let spec = self.catalog.get(&task_id).ok_or_else(|| {
            Status::not_found(format!("task {task_id} not in catalog"))
        })?;

        let env_id = self
            .registry
            .register_booting(&task_id)
            .await
            .map_err(Self::registry_status)?;

        Self::spawn_boot_task(
            Arc::clone(&self.registry),
            env_id.clone(),
            spec.clone(),
        );

        Ok(Response::new(CreateEnvironmentResponse {
            env_id,
            manager_addr: self.manager_addr.clone(),
            initial_messages_json: spec.initial_messages_json.clone(),
            tools_json: spec.tools_json.clone(),
        }))
    }

    async fn execute(
        &self,
        request: Request<ExecuteRequest>,
    ) -> Result<Response<ExecuteResponse>, Status> {
        let request = request.into_inner();
        let env_id = request.env_id;

        if request.tool_name == SUBMIT_TOOL {
            match self.registry.mark_submitted(&env_id).await {
                Ok(()) => {
                    return Ok(Response::new(ExecuteResponse {
                        content: "Submission received. Your solution will be graded.".into(),
                        is_error: false,
                    }));
                }
                Err(RegistryError::AlreadySubmitted { .. }) => {
                    return Ok(Response::new(ExecuteResponse {
                        content: "already submitted".into(),
                        is_error: true,
                    }));
                }
                Err(err) => return Err(Self::registry_status(err)),
            }
        }

        self.registry
            .require_execute(&env_id)
            .await
            .map_err(Self::registry_status)?;

        // TODO: forward ExecuteRequest to the in-VM executor over vsock and relay
        // its ExecuteResponse.
        Ok(Response::new(ExecuteResponse {
            content: format!(
                "{} is not implemented for env {}: {}",
                request.tool_name, env_id, request.arguments_json
            ),
            is_error: true,
        }))
    }

    async fn evaluate(
        &self,
        request: Request<EvaluateRequest>,
    ) -> Result<Response<EvaluateResponse>, Status> {
        let env_id = request.into_inner().env_id;

        self.registry
            .require_evaluate(&env_id)
            .await
            .map_err(Self::registry_status)?;

        // TODO: send an Evaluate frame to this env's in-VM executor over vsock and
        // return the response it produces. Until forwarding exists, surface an
        // infra error so GRPO excludes this rollout.
        self.registry
            .mark_evaluated(&env_id)
            .await
            .map_err(Self::registry_status)?;

        Ok(Response::new(EvaluateResponse {
            reward: 0.0,
            detail_json: format!(
                "{{\"error\":\"evaluate forwarding not implemented for env {env_id}\"}}"
            ),
            infra_error: true,
        }))
    }

    async fn teardown(
        &self,
        request: Request<TeardownRequest>,
    ) -> Result<Response<TeardownResponse>, Status> {
        let env_id = request.into_inner().env_id;
        if let Some(vm) = self.registry.take_vm(&env_id).await {
            vm.stop().await;
        }
        let _ = self.registry.remove(&env_id).await;
        Ok(Response::new(TeardownResponse {}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::catalog::Catalog;
    use std::sync::Arc;

    fn test_catalog() -> Arc<Catalog> {
        let jsonl = concat!(
            r#"{"task_id":"t1","split":"dev","messages":[{"role":"user","content":"hi"}],"tools":[],"base_image":"images/bases/t.ext4","task_image":"images/tasks/t1.ext4"}"#,
            "\n",
        );
        Arc::new(Catalog::from_jsonl(jsonl).unwrap())
    }

    fn test_service(max_concurrent: usize) -> EnvironmentServiceImpl {
        EnvironmentServiceImpl::with_registry(
            test_catalog(),
            Arc::new(Registry::with_capacity(max_concurrent)),
            "127.0.0.1:50051",
        )
    }

    #[tokio::test]
    async fn create_returns_manager_addr_and_registers_env() {
        let svc = test_service(4);
        let resp = svc
            .create_environment(Request::new(CreateEnvironmentRequest {
                task_id: "t1".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.manager_addr, "127.0.0.1:50051");
        assert!(resp.env_id.starts_with("t1-"));
        // Boot task transitions to Ready quickly.
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        assert_eq!(
            svc.registry.phase(&resp.env_id).await.unwrap(),
            crate::registry::EnvPhase::Ready
        );
    }

    #[tokio::test]
    async fn admission_exhausted_on_create() {
        let svc = test_service(1);
        svc.create_environment(Request::new(CreateEnvironmentRequest {
            task_id: "t1".into(),
        }))
        .await
        .unwrap();
        let err = svc
            .create_environment(Request::new(CreateEnvironmentRequest {
                task_id: "t1".into(),
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::ResourceExhausted);
    }

    #[tokio::test]
    async fn execute_after_evaluate_fails() {
        let svc = test_service(4);
        let env_id = svc
            .create_environment(Request::new(CreateEnvironmentRequest {
                task_id: "t1".into(),
            }))
            .await
            .unwrap()
            .into_inner()
            .env_id;
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;

        svc.evaluate(Request::new(EvaluateRequest {
            env_id: env_id.clone(),
        }))
        .await
        .unwrap();

        let err = svc
            .execute(Request::new(ExecuteRequest {
                env_id: env_id.clone(),
                tool_name: "bash".into(),
                arguments_json: r#"{"command":"echo hi"}"#.into(),
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::FailedPrecondition);
    }

    #[tokio::test]
    async fn submit_twice_returns_error_content() {
        let svc = test_service(4);
        let env_id = svc
            .create_environment(Request::new(CreateEnvironmentRequest {
                task_id: "t1".into(),
            }))
            .await
            .unwrap()
            .into_inner()
            .env_id;
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;

        let first = svc
            .execute(Request::new(ExecuteRequest {
                env_id: env_id.clone(),
                tool_name: SUBMIT_TOOL.into(),
                arguments_json: "{}".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(!first.is_error);

        let second = svc
            .execute(Request::new(ExecuteRequest {
                env_id,
                tool_name: SUBMIT_TOOL.into(),
                arguments_json: "{}".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(second.is_error);
        assert!(second.content.contains("already submitted"));
    }

    #[tokio::test]
    async fn teardown_removes_env() {
        let svc = test_service(4);
        let env_id = svc
            .create_environment(Request::new(CreateEnvironmentRequest {
                task_id: "t1".into(),
            }))
            .await
            .unwrap()
            .into_inner()
            .env_id;

        svc.teardown(Request::new(TeardownRequest { env_id: env_id.clone() }))
            .await
            .unwrap();

        let err = svc
            .execute(Request::new(ExecuteRequest {
                env_id,
                tool_name: "bash".into(),
                arguments_json: "{}".into(),
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }
}
