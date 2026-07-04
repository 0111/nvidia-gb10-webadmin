import { onUnmounted } from 'vue'
import { useWsStore } from '@/stores/ws'

/**
 * Subscribe a component to a WS topic on the shared connection. Automatically
 * unsubscribes when the owning component unmounts. Does not open/close the
 * underlying socket — that is owned by the ws store and started once after
 * login (see App.vue).
 */
export function useWebSocketTopic<T = any>(topic: string, callback: (data: T) => void): void {
  const wsStore = useWsStore()
  const unsubscribe = wsStore.subscribe(topic, callback)
  onUnmounted(() => {
    unsubscribe()
  })
}
