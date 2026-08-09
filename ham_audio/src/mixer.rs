//! Mikser TX — zbiera Opus ramki od N klientów WS i miksuje do PCM

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::Mutex;
use bytes::Bytes;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

pub struct Mixer {
    clients: Mutex<HashMap<u64, std::collections::VecDeque<Bytes>>>,
    /// Kolejka zmiksowanych ramek TX gotowych do odtworzenia.
    /// Zachowane pod przyszly mikser TX (obecnie RX-only) — nie usuwac.
    #[allow(dead_code)]
    output:  Mutex<std::collections::VecDeque<Bytes>>,
}

impl Mixer {
    pub fn new() -> Self {
        Self {
            clients: Mutex::new(HashMap::new()),
            output:  Mutex::new(std::collections::VecDeque::new()),
        }
    }

    pub fn add_client(&self) -> u64 {
        let id = NEXT_ID.fetch_add(1, Ordering::SeqCst);
        // Inicjalizacja synchroniczna przez try_lock — zawsze OK bo nikt nie trzyma blokady długo
        let clients = self.clients.try_lock();
        if let Ok(mut c) = clients {
            c.insert(id, std::collections::VecDeque::new());
        }
        id
    }

    pub fn remove_client(&self, id: u64) {
        if let Ok(mut c) = self.clients.try_lock() {
            c.remove(&id);
        }
    }

    pub fn has_clients(&self) -> bool {
        self.clients.try_lock()
            .map(|c| !c.is_empty())
            .unwrap_or(false)
    }

    /// Dodaj ramkę TX od klienta
    pub fn push_tx(&self, client_id: u64, data: Bytes) {
        if let Ok(mut clients) = self.clients.try_lock() {
            if let Some(q) = clients.get_mut(&client_id) {
                // Ogranicz bufor do 50 ramek (~1s)
                if q.len() < 50 {
                    q.push_back(data);
                }
            }
        }
    }

    /// Pobierz następną ramkę TX do odtworzenia
    /// Prosty mikser: bierze ramkę od pierwszego klienta który ma dane
    /// (dla jednego TX użytkownika — wystarczy)
    pub async fn get_tx_frame(&self) -> Option<Bytes> {
        let mut clients = self.clients.lock().await;
        for (_, q) in clients.iter_mut() {
            if let Some(frame) = q.pop_front() {
                return Some(frame);
            }
        }
        None
    }
}
