"""Azure Function: refreshes the programme documents in the agent's vector store every day at 06:00 UTC."""
import logging

import azure.functions as func

import docs_sync

app = func.FunctionApp()


@app.timer_trigger(schedule="0 0 6 * * *", arg_name="timer")
def refresh_documents(timer: func.TimerRequest):
    logging.info("Document refresh: %s", docs_sync.sync())
