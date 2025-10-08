from typing import TypedDict, Annotated , Optional
from langgraph.graph import StateGraph , START ,END
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
import os
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import HumanMessage , AIMessage , ToolMessage, AIMessageChunk
from langgraph.checkpoint.memory import MemorySaver
from uuid import uuid4
from fastapi import FastAPI , Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
import json



load_dotenv("../.env")
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
os.environ["TAVILY_API_KEY"] = os.getenv("TAVILY_API_KEY")

model = ChatOpenAI(model="gpt-4o-mini", temperature=0)
search_tool = TavilySearchResults(max_result=4)

tools = [search_tool]
llm_with_tools = model.bind_tools(tools)
memory = MemorySaver()

class State(TypedDict):
    messages: list[BaseMessage]

async def model(state:State):
    result = await llm_with_tools.ainvoke(state["messages"])
    return {
        "messages":[result],
    }

async def tools_router(state:State):
    last_message = state["messages"][-1]
    if (hasattr(last_message,"tool_calls") and len(last_message.tool_calls)>0):
        return "tool_node"
    else:
        return "__end__"
    
async def tool_node(state):

    tool_calls = state["messages"][-1].tool_calls
    tool_messages = []

    for tool_call in tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]
        tool_id = tool_call["id"]

        if tool_name == "tavily_search_results_json":
            search_results = await search_tool.ainvoke(tool_args)
            tool_message = ToolMessage(
                content = search_results,
                tool_call_id = tool_id,
                name = tool_name
            )
            tool_messages.append(tool_message)
    return {
        "messages": tool_messages
    }

graph_builder = StateGraph(State)
graph_builder.add_node("model",model)
graph_builder.add_node("tool_node",tool_node)
graph_builder.set_entry_point("model")
graph_builder.add_conditional_edges("model",tools_router,{"tool_node":"tool_node","__end__":"__end__"})
graph_builder.add_edge("tool_node","model")
graph = graph_builder.compile(checkpointer = memory)



app =FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins = ["*"],
    allow_credentials = True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers = ["content-type"]
)

def serialize_ai_message_chunks(chunk):
    if (isinstance(chunk,AIMessageChunk)):
        return chunk.content
    else:
        raise TypeError(
            f"Object of type {type(chunk).__name__} is not correctly formatted for serialisation"
        )

async def generate_chat_responses(messages:str ,checkpoint_id:Optional[str] = None):
    is_new_conversation = checkpoint_id is None

    if is_new_conversation:
        new_checkpoint_id = str(uuid4())
        yield f"data: {{\"type\": \"checkpoint\", \"checkpoint_id\": \"{new_checkpoint_id}\"}}\n\n"
    else:
        new_checkpoint_id = checkpoint_id
    
    try:
        # Simple approach: use LLM directly
        human_msg = HumanMessage(content=messages)
        
        # Get response from LLM with tools
        response = await llm_with_tools.ainvoke([human_msg])
        
        # Check if there are tool calls
        if hasattr(response, "tool_calls") and response.tool_calls:
            for tool_call in response.tool_calls:
                if tool_call["name"] == "tavily_search_results_json":
                    search_query = tool_call["args"].get("query", "")
                    safe_query = search_query.replace('"', '\\"').replace("'", "\\'").replace("\n", "\\n")
                    yield f"data: {{\"type\": \"search_start\", \"query\": \"{safe_query}\"}}\n\n"
                    
                    # Execute search
                    try:
                        search_results = await search_tool.ainvoke(tool_call["args"])
                        if isinstance(search_results, list):
                            urls = []
                            for item in search_results:
                                if isinstance(item, dict) and "url" in item:
                                    urls.append(item["url"])
                            
                            urls_json = json.dumps(urls)
                            yield f"data: {{\"type\": \"search_results\", \"urls\": {urls_json}}}\n\n"
                            
                            # Get final response with search results
                            final_response = await llm_with_tools.ainvoke([
                                human_msg,
                                response,
                                ToolMessage(content=str(search_results), tool_call_id=tool_call["id"])
                            ])
                            
                            if hasattr(final_response, "content") and final_response.content:
                                safe_content = final_response.content.replace("'", "\\'").replace("\n", "\\n")
                                yield f"data: {{\"type\": \"content\", \"content\": \"{safe_content}\"}}\n\n"
                                
                    except Exception as e:
                        print(f"Search error: {e}")
                        yield f"data: {{\"type\": \"search_error\", \"error\": \"{str(e)}\"}}\n\n"
        else:
            # No tool calls, just return the response
            if hasattr(response, "content") and response.content:
                safe_content = response.content.replace("'", "\\'").replace("\n", "\\n")
                yield f"data: {{\"type\": \"content\", \"content\": \"{safe_content}\"}}\n\n"
                    
    except Exception as e:
        print(f"Error in streaming: {e}")
        yield f"data: {{\"type\": \"error\", \"error\": \"{str(e)}\"}}\n\n"
    
    # Send an end event
    yield f"data: {{\"type\": \"end\"}}\n\n"

@app.get("/chat_stream/{message}")
async def chat_stream(message: str, checkpoint_id: Optional[str] = Query(None)):
    return StreamingResponse(
        generate_chat_responses(message, checkpoint_id), 
        media_type="text/event-stream"
    )
