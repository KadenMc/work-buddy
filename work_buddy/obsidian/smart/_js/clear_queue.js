// Clear stale messages from the embed model's message queue.
// Must be called before any operation that triggers embedding.
const em = window.smart_env?.smart_sources?.embed_model;
if (em?.message_queue) {
    for (const k of Object.keys(em.message_queue)) {
        em.message_queue[k]?.reject?.(new Error('stale'));
        delete em.message_queue[k];
    }
}
