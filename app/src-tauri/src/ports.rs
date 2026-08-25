//! Loopback port allocation for the backing stack.
//!
//! The defaults deliberately avoid every upstream default (6333, 5432, 7233,
//! 7687, 8080) so the stack cannot collide with a Qdrant, Postgres or Memgraph
//! the user already runs — including the `sociologia-qdrant` container in
//! `docaget/`. When a
//! preferred port is taken anyway, the next free one in a small window is used
//! and the whole assignment is persisted, because a port that changed between
//! launches would orphan the containers published on the old one.

use serde::{Deserialize, Serialize};
use std::net::{Ipv4Addr, TcpListener};

use crate::error::{AppError, Result};

/// How far past a preferred port to search before giving up.
const SEARCH_WINDOW: u16 = 40;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Ports {
    pub qdrant_http: u16,
    pub qdrant_grpc: u16,
    pub memgraph: u16,
    pub postgres: u16,
    pub temporal: u16,
    pub temporal_ui: u16,
    pub api: u16,
}

impl Default for Ports {
    fn default() -> Self {
        Self {
            qdrant_http: 6433,
            qdrant_grpc: 6434,
            memgraph: 7788,
            postgres: 5532,
            temporal: 7333,
            temporal_ui: 8380,
            api: 8787,
        }
    }
}

impl Ports {
    pub fn as_pairs(&self) -> [(&'static str, u16); 7] {
        [
            ("BRAIN_QDRANT_HTTP_PORT", self.qdrant_http),
            ("BRAIN_QDRANT_GRPC_PORT", self.qdrant_grpc),
            ("BRAIN_MEMGRAPH_PORT", self.memgraph),
            ("BRAIN_POSTGRES_PORT", self.postgres),
            ("BRAIN_TEMPORAL_PORT", self.temporal),
            ("BRAIN_TEMPORAL_UI_PORT", self.temporal_ui),
            // The host-published port, not the one uvicorn binds inside the
            // container — see the comment on the api service in the compose file.
            ("BRAIN_API_HOST_PORT", self.api),
        ]
    }
}

/// Whether a port can be bound on loopback right now.
///
/// This is a probe, not a reservation: the port could be taken between this
/// check and compose publishing it. That race is acceptable — compose fails
/// loudly with a bind error, which is a better outcome than holding sockets
/// open across a container start.
fn is_free(port: u16) -> bool {
    TcpListener::bind((Ipv4Addr::LOCALHOST, port)).is_ok()
}

fn allocate_one(purpose: &str, preferred: u16, taken: &[u16]) -> Result<u16> {
    for candidate in preferred..preferred.saturating_add(SEARCH_WINDOW) {
        if !taken.contains(&candidate) && is_free(candidate) {
            return Ok(candidate);
        }
    }
    Err(AppError::NoFreePort {
        purpose: purpose.to_string(),
        start: preferred,
        end: preferred.saturating_add(SEARCH_WINDOW),
    })
}

/// Allocate a full set, starting from the preferred defaults.
///
/// Ports already assigned within this call are excluded from later searches so
/// two services cannot be handed the same number.
pub fn allocate(preferred: Ports) -> Result<Ports> {
    let mut taken: Vec<u16> = Vec::with_capacity(7);
    let next = |purpose: &str, want: u16, taken: &mut Vec<u16>| -> Result<u16> {
        let got = allocate_one(purpose, want, taken)?;
        taken.push(got);
        Ok(got)
    };

    Ok(Ports {
        qdrant_http: next("qdrant http", preferred.qdrant_http, &mut taken)?,
        qdrant_grpc: next("qdrant grpc", preferred.qdrant_grpc, &mut taken)?,
        memgraph: next("memgraph bolt", preferred.memgraph, &mut taken)?,
        postgres: next("postgres", preferred.postgres, &mut taken)?,
        temporal: next("temporal", preferred.temporal, &mut taken)?,
        temporal_ui: next("temporal ui", preferred.temporal_ui, &mut taken)?,
        api: next("control api", preferred.api, &mut taken)?,
    })
}

/// Re-check a persisted assignment, keeping ports that are still usable.
///
/// A port held by *our own* running container reads as taken, which is why this
/// is only called when the stack is down. Reallocating under a running stack
/// would publish a second set of bindings and leave the first orphaned.
pub fn revalidate(saved: Ports) -> Result<Ports> {
    if saved.as_pairs().iter().all(|(_, p)| is_free(*p)) {
        return Ok(saved);
    }
    allocate(saved)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_avoid_every_upstream_default() {
        let p = Ports::default();
        let upstream = [6333, 6334, 5432, 7233, 7687, 8080, 8000];
        for (_, port) in p.as_pairs() {
            assert!(
                !upstream.contains(&port),
                "port {port} collides with an upstream default"
            );
        }
    }

    #[test]
    fn allocation_never_repeats_a_port() {
        let p = allocate(Ports::default()).expect("allocation");
        let mut seen: Vec<u16> = p.as_pairs().iter().map(|(_, v)| *v).collect();
        seen.sort_unstable();
        let before = seen.len();
        seen.dedup();
        assert_eq!(before, seen.len(), "duplicate port in {p:?}");
    }

    #[test]
    fn an_occupied_preferred_port_is_skipped() {
        let held = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("bind");
        let occupied = held.local_addr().expect("addr").port();

        let preferred = Ports {
            qdrant_http: occupied,
            ..Ports::default()
        };
        let got = allocate(preferred).expect("allocation");
        assert_ne!(got.qdrant_http, occupied);
        assert!(got.qdrant_http > occupied);
    }

    #[test]
    fn revalidate_keeps_a_still_free_assignment() {
        let free = allocate(Ports::default()).expect("allocation");
        assert_eq!(revalidate(free).expect("revalidate"), free);
    }
}
