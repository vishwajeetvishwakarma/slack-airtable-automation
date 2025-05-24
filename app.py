import os
import hmac
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pyairtable import Table
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
import uvicorn

load_dotenv()

app = FastAPI(
    title="Airtable-Slack Dependency Notifier",
    description="A webhook-based application to notify Slack users when their Airtable tasks are unblocked",
    version="1.0.0"
)

class WebhookPayload(BaseModel):
    base: Optional[Dict[str, Any]] = None
    webhook: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = None
    test: Optional[bool] = False
    changedTablesById: Optional[Dict[str, Dict[str, Any]]] = None

    class Config:
        schema_extra = {
            "example": {
                "base": {"id": "appXXXXXXXXXXXXXX"},
                "webhook": {"id": "webhookXXXXXXXXXXXXXX"},
                "timestamp": "2023-05-22T12:00:00.000Z",
                "test": False,
                "changedTablesById": {
                    "Tasks": {
                        "changedRecordsById": {
                            "recXXXXXXXXXXXXXX": {
                                "current": {
                                    "cellValuesByFieldId": {"Status": "Done"}
                                }
                            }
                        }
                    }
                }
            }
        }

class DependencyNotifier:
    def __init__(self):
        """
        here we are initializing the airtable and slack clients
        """
        self.airtable_api_key = os.getenv("AIRTABLE_API_KEY")
        self.airtable_base_id = os.getenv("AIRTABLE_BASE_ID")
        self.airtable_table_name = os.getenv("AIRTABLE_TABLE_NAME")
        
        # Slack configuration
        self.slack_bot_token = os.getenv("SLACK_BOT_TOKEN")
        self.slack_channel_id = os.getenv("SLACK_CHANNEL_ID")
        
        # Webhook configuration
        self.webhook_secret = os.getenv("AIRTABLE_WEBHOOK_SECRET", "")
        self.airtable = Table(self.airtable_api_key, self.airtable_base_id, self.airtable_table_name)
        self.slack_client = WebClient(token=self.slack_bot_token)
        
        print(f"Initialized DependencyNotifier")
        print(f"Connected to Airtable base: {self.airtable_base_id}, table: {self.airtable_table_name}")
    
    async def get_all_tasks(self) -> List[Dict[str, Any]]:
        """Get all tasks from Airtable"""
        try:
            return self.airtable.all()
        except Exception as e:
            print(f"Error fetching tasks from Airtable: {e}")
            return []
    
    async def get_task_by_id(self, task_id: str) -> Dict[str, Any]:
        """Get a single task by ID"""
        try:
            return self.airtable.get(task_id)
        except Exception as e:
            print(f"Error fetching task {task_id}: {e}")
            return None
    
    async def find_dependent_tasks(self, completed_task_id: str) -> List[Dict[str, Any]]:
        """Find tasks that depend on the completed task"""
        all_tasks = await self.get_all_tasks()
        dependent_tasks = []
        
        for task in all_tasks:
            if task['fields'].get('Status') == 'Done':
                continue
            dependencies_str = task['fields'].get('Dependencies', '')
            if not dependencies_str:
                continue
            dependencies = [dep.strip() for dep in dependencies_str.split(',')]
            if completed_task_id in dependencies:
                dependent_tasks.append(task)
        
        return dependent_tasks
    
    async def update_task_status(self, task_id: str, new_status: str) -> Dict[str, Any]:
        try:
            return self.airtable.update(task_id, {'Status': new_status})
        except Exception as e:
            print(f"Error updating task status: {e}")
            return None
    
    async def send_slack_notification(self, user_id: str, message: str) -> Dict[str, Any]:
        """Send a notification to a user in Slack"""
        try:
            response = self.slack_client.chat_postMessage(
                channel=self.slack_channel_id,
                text=f"<@{user_id}> {message}"
            )
            return response
        except SlackApiError as e:
            print(f"Error sending Slack message: {e.response['error']}")
            return None
    
    async def log_notification(self, task_id: str, message: str) -> Dict[str, Any]:
        """Log that a notification was sent back to Airtable"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            return self.airtable.update(task_id, {
                'Last Notification': message,
                'Last Notified': timestamp
            })
        except Exception as e:
            print(f"Error logging notification: {e}")
            return None
    
    async def process_completed_task(self, task_id: str) -> None:
        """Process a single completed task and notify dependents"""
        task = await self.get_task_by_id(task_id)
        
        if not task or task['fields'].get('Status') != 'Done':
            print(f"Task {task_id} is not completed or doesn't exist")
            return
            
        task_name = task['fields'].get('Name', 'Unnamed task')
        print(f"Processing completed task: {task_name} ({task_id})")
        
        dependent_tasks = await self.find_dependent_tasks(task_id)
        print(f"Found {len(dependent_tasks)} tasks that depend on this completed task")
        
        for dependent_task in dependent_tasks:
            dep_id = dependent_task['id']
            dep_name = dependent_task['fields'].get('Name', 'Unnamed task')
            assignee = dependent_task['fields'].get('Assignee')
            
            await self.update_task_status(dep_id, 'Ready')
            print(f"Updated task '{dep_name}' status to Ready")
            
            if assignee:
                message = f"Your task '{dep_name}' is now ready to work on! All dependencies have been completed."
                await self.send_slack_notification(assignee, message)
                
                await self.log_notification(dep_id, message)
                
                print(f"Notified {assignee} about task '{dep_name}'")

notifier = DependencyNotifier()

async def verify_signature(request: Request) -> bool:
    if not notifier.webhook_secret:
        return True 
        
    signature = request.headers.get('x-airtable-content-signature', '')
    body = await request.body()
    
    expected_signature = hmac.new(
        notifier.webhook_secret.encode('utf-8'),
        body,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected_signature)

@app.post("/webhook", status_code=status.HTTP_200_OK)
async def webhook_handler(
    request: Request,
    payload: WebhookPayload,
    background_tasks: BackgroundTasks
):
    is_valid = await verify_signature(request)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature"
        )

    if payload.test:
        return {"success": True, "message": "Test webhook received"}
    
    if (payload.base and 
        payload.base.get('id') == notifier.airtable_base_id and
        payload.changedTablesById):
        
        print("Received webhook from our Airtable base")
        
        changed_tables = payload.changedTablesById
        if notifier.airtable_table_name in changed_tables:
            changed_records = changed_tables.get(notifier.airtable_table_name, {}).get('changedRecordsById', {})
            
            for record_id, change_data in changed_records.items():
                if change_data.get('current', {}).get('cellValuesByFieldId', {}).get('Status') == 'Done':
                    print(f"Task {record_id} was marked as Done")
                    background_tasks.add_task(notifier.process_completed_task, record_id)
    
    return {"success": True}

@app.get("/", status_code=status.HTTP_200_OK)
async def root():
    return {
        "status": "ok",
        "message": "Airtable-Slack Dependency Notifier is running",
        "airtable_base": notifier.airtable_base_id,
        "airtable_table": notifier.airtable_table_name
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"Starting webhook server on port {port}")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)