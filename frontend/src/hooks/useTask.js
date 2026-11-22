import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { isTerminal } from '../api/contracts'

const POLL_INTERVAL_MS = 1500

// Polls GET /api/tasks/{id} and /events until the task reaches a terminal status.
// Render the consumer with key={taskId} so state resets when the task changes.
export function useTask(taskId) {
  const [task, setTask] = useState(null)
  const [events, setEvents] = useState([])
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const [nextTask, nextEvents] = await Promise.all([api.getTask(taskId), api.getTaskEvents(taskId)])
      setTask(nextTask)
      setEvents(Array.isArray(nextEvents) ? nextEvents : [])
      setError(null)
      return nextTask
    } catch (err) {
      setError(err)
      return null
    }
  }, [taskId])

  useEffect(() => {
    let stopped = false
    let timer
    async function poll() {
      const latest = await refresh()
      if (!stopped && !(latest && isTerminal(latest.status))) timer = setTimeout(poll, POLL_INTERVAL_MS)
    }
    poll()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [refresh])

  return { task, events, error, refresh, setTask }
}