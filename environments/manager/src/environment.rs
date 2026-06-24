use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use tonic::{Request, Response, Status};

use crate::catalog::Catalog;
use crate::pb::environment_service_server::EnvironmentService;
use crate::pb::{
    CloseRequest, CloseResponse, CreateEnvironmentRequest, CreateEnvironmentResponse,
    ExecuteRequest, ExecuteResponse, ResetRequest, ResetResponse, ScoreRequest, ScoreResponse,
};

#[derive(Debug, Default)]
pub struct EnvironmentServiceImpl {
    /// Per-task prompts/tools, loaded from the environment's tasks.jsonl. The
    /// manager serves these verbatim; it does not interpret them.
    catalog: Arc<Catalog>,
    /// Monotonic suffix making env_ids unique within this manager process.
    next_env: AtomicU64,
}

impl EnvironmentServiceImpl {
    pub fn new(catalog: Arc<Catalog>) -> Self {
        Self {
            catalog,
            next_env: AtomicU64::new(0),
        }
    }

    fn new_env_id(&self, task_id: &str) -> String {
        let n = self.next_env.fetch_add(1, Ordering::Relaxed);
        format!("{task_id}-{n}")
    }
}

#[tonic::async_trait]
impl EnvironmentService for EnvironmentServiceImpl {
    async fn create_environment(
        &self,
        request: Request<CreateEnvironmentRequest>,
    ) -> Result<Response<CreateEnvironmentResponse>, Status> {
        let task_id = request.into_inner().task_id;
        let spec = self.catalog.get(&task_id).ok_or_else(|| {
            Status::not_found(format!("task {task_id} not in catalog"))
        })?;

        // The trainer needs the opening prompt and tools to drive the policy;
        // the environment is their source of truth, so we return them here.
        // TODO: boot the Firecracker VM for this task and set manager_addr from
        // GRL_MANAGER_ADVERTISE_ADDR so clients dial this instance directly;
        // return RESOURCE_EXHAUSTED when this node's VM slots are full so the
        // client retries on a fresh connection (kube-proxy rebalances it).
        Ok(Response::new(CreateEnvironmentResponse {
            env_id: self.new_env_id(&task_id),
            manager_addr: String::new(),
            initial_messages_json: spec.initial_messages_json.clone(),
            tools_json: spec.tools_json.clone(),
        }))
    }

    async fn execute(
        &self,
        request: Request<ExecuteRequest>,
    ) -> Result<Response<ExecuteResponse>, Status> {
        let request = request.into_inner();
        // TODO: forward this ExecuteRequest to the in-VM executor over vsock and
        // relay its ExecuteResponse.
        Ok(Response::new(ExecuteResponse {
            content: format!(
                "{} is not implemented for env {}: {}",
                request.tool_name, request.env_id, request.arguments_json
            ),
            is_error: true,
        }))
    }

    async fn score(
        &self,
        request: Request<ScoreRequest>,
    ) -> Result<Response<ScoreResponse>, Status> {
        let env_id = request.into_inner().env_id;
        // The reward is computed inside the env executor (it runs the held-out
        // test suite against the policy's edits); the manager only relays it.
        // TODO: send a Score frame to this env's in-VM executor over vsock and
        // return the ScoreResponse it produces (see env/src/score.rs). Until the
        // vsock transport exists, report a zero reward rather than failing the
        // trajectory.
        Ok(Response::new(ScoreResponse {
            reward: 0.0,
            detail_json: format!(
                "{{\"error\":\"score forwarding not implemented for env {env_id}\"}}"
            ),
        }))
    }

    async fn reset(
        &self,
        request: Request<ResetRequest>,
    ) -> Result<Response<ResetResponse>, Status> {
        let env_id = request.into_inner().env_id;
        Err(Status::unimplemented(format!(
            "Reset is not implemented for env {env_id}"
        )))
    }

    async fn close(
        &self,
        request: Request<CloseRequest>,
    ) -> Result<Response<CloseResponse>, Status> {
        let env_id = request.into_inner().env_id;
        // TODO: tear down the VM for this env. Treated as best-effort so the
        // client's cleanup path never fails.
        let _ = env_id;
        Ok(Response::new(CloseResponse {}))
    }
}
