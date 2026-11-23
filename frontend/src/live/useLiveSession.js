import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { createLiveState, liveReducer } from './liveState'
import { ConnectionStatus } from './transport'

// Opens one live connection per task while mounted and always closes it on unmount or task change.
// Mount the consumer with key={taskId} so state starts fresh for each task.
export function useLiveSession(taskId, service) {
  const [state, dispatch] = useReducer(liveReducer, undefined, createLiveState)
  const [status, setStatus] = useState(service.capabilities.liveEvents ? ConnectionStatus.CONNECTING : ConnectionStatus.UNAVAILABLE)
  const connection = useRef(null)

  useEffect(() => {
    const conn = service.connect(taskId, {
      onEvents: (events) => dispatch({ type: 'events', events }),
      onStatus: setStatus,
    })
    connection.current = conn
    return () => {
      conn.close()
      connection.current = null
    }
  }, [taskId, service])

  const retry = useCallback(() => connection.current?.retry(), [])
  return { state, status, retry }
}