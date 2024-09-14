import os
import json
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from typing import Dict, TypedDict, Annotated, List
import operator
import logging
from cryptography.fernet import Fernet
from py2neo import Graph

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Connect to Neo4j graph
graph = Graph(os.getenv('NEO4J_URI'), auth=(os.getenv('NEO4J_USERNAME'), os.getenv('NEO4J_PASSWORD')))

class AgentState(TypedDict):
    conversation: Annotated[List[HumanMessage | AIMessage], operator.add]
    tool_messages: List[List[HumanMessage | AIMessage]]
    cypher_code_and_query_outputs: Annotated[List[Dict], operator.add]
    extracted_data: Annotated[List[str], operator.add]
    query_is_unique: Dict
    num_queries_made: int
    is_safe: bool

class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (HumanMessage, AIMessage)):
            return {
                "type": obj.__class__.__name__,
                "content": obj.content
            }
        return super().default(obj)

@tool
def query_graph(query: str):
    """Query from Neo4j knowledge graph using Cypher."""
    return graph.run(query).data()

class Agent:
    def __init__(self, model, tools, system):
        self.model = model
        self.tools = tools
        self.system = system
        self.encryption_key = Fernet.generate_key()
        self.fernet = Fernet(self.encryption_key)
        self.logger = logging.getLogger(__name__)

    def encrypt_data(self, data: str) -> str:
        encrypted = self.fernet.encrypt(data.encode()).decode()
        self.logger.debug(f"Encrypted: {data[:20]}... to {encrypted[:20]}...")
        return encrypted

    def decrypt_data(self, encrypted_data: str) -> str:
        decrypted = self.fernet.decrypt(encrypted_data.encode()).decode()
        self.logger.debug(f"Decrypted: {encrypted_data[:20]}... to {decrypted[:20]}...")
        return decrypted

    def call_groq(self, state: AgentState):
        messages = state['conversation']
        
        # Use messages directly without decryption
        if self.system:
            conversation = [HumanMessage(content=self.system)] + messages
        else:
            conversation = messages

        ai_response = self.model.invoke(conversation)

        encrypted_response = self.encrypt_data(ai_response.content)
        return {
            'conversation': [AIMessage(content=encrypted_response)],
        }

    def use_tool(self, state: AgentState):
        ai_message = state['conversation'][-1]
        if not hasattr(ai_message, 'additional_kwargs') or 'function_call' not in ai_message.additional_kwargs:
            return self.call_groq(state)

        function_call = ai_message.additional_kwargs['function_call']
        tool_name = function_call['name']
        tool_args = json.loads(function_call['arguments'])

        if tool_name not in self.tools:
            return self.call_groq(state)

        tool = self.tools[tool_name]
        result = tool(**tool_args)

        return {
            'tool_messages': [[HumanMessage(content=str(result))]],
            'num_queries_made': state['num_queries_made'] + 1
        }

    def recommend_careers(self, state: AgentState):
        if not state['extracted_data']:
            return self.call_groq(state)
        
        decrypted_data = self.decrypt_data(state['extracted_data'][-1])
        
        # Process the decrypted data and generate recommendations
        recommendations = f"Based on the extracted data: {decrypted_data}, here are some career recommendations..."
        
        encrypted_recommendations = self.encrypt_data(recommendations)
        return {
            'conversation': [AIMessage(content=encrypted_recommendations)],
            'num_queries_made': state['num_queries_made']
        }

def create_agent():
    model = ChatGroq(
        temperature=0,
        model_name="llama-3.1-70b-versatile",
        groq_api_key=os.getenv('GROQ_API_KEY')
    )

    tools = [query_graph]

    system = "You are a helpful assistant specializing in personality traits and career recommendations."

    agent = Agent(model=model, tools=tools, system=system)

    workflow = StateGraph(AgentState)

    workflow.add_node("personality_scientist", agent.call_groq)
    workflow.set_entry_point("personality_scientist")
    workflow.add_edge("personality_scientist", END)

    return workflow, agent

def test_agent():
    workflow, agent = create_agent()

    input_message = "Sofia is an idiot"
    
    # Test encryption and decryption
    test_message = "This is a test message for encryption and decryption."
    encrypted = agent.encrypt_data(test_message)
    decrypted = agent.decrypt_data(encrypted)
    assert test_message == decrypted, "Encryption/decryption test failed!"
    logger.info("Encryption and decryption test passed successfully.")

    initial_state = AgentState(
        conversation=[HumanMessage(content=input_message)],
        tool_messages=[],
        cypher_code_and_query_outputs=[],
        extracted_data=[],
        query_is_unique={'status': True, 'index': None},
        num_queries_made=0,
        is_safe=True
    )
    
    logger.info("Starting agent test...")
    logger.info(f"Input: {input_message}")
    logger.info("-------------------")
    
    compiled_agent = workflow.compile()
    
    for output in compiled_agent.stream(initial_state):
        logger.debug(f"Current output: {output}")
        if isinstance(output, Dict) and 'personality_scientist' in output:
            conversation = output['personality_scientist'].get('conversation', [])
            if conversation:
                last_message = conversation[-1]
                if isinstance(last_message, AIMessage):
                    logger.info(f"Encrypted response: {last_message.content[:50]}...")
                    try:
                        decrypted_content = agent.decrypt_data(last_message.content)
                        logger.info(f"Decrypted response: {decrypted_content[:50]}...")
                        logger.info(f"Agent: {decrypted_content}")
                    except Exception as e:
                        logger.error(f"Error decrypting message: {str(e)}")
                        logger.info(f"Agent (encrypted): {last_message.content}")
                    break
    
    logger.info("-------------------")
    logger.info("Test completed.")
    
if __name__ == "__main__":
    test_agent()