build a chatbot agent with web interface demo
the agent will have access to 2 tools invoice_info and return_money
the goal of the demo is to show how the agent is able to:
- return money more that 1 time if it is not using dogwood as the policy as code solution
- skip invoice_info and jump directly to the return money if it is not using dogwood as the policy as code solution
 for this reason it should have some type of toggle to activate the usage of dogwood or not


LLM connection details:

azure URL: https://aifoundry06810.services.ai.azure.com/models
model deployment: Phi-4-mini-instruct
authen tication should be done using Default Credentials


Steps:
- create a plan FIRST
- create your policies
- create the web agent demo
- write a short miningful README.md

Tips:
- KISS
- python and uv
- all configs via env variables
- dogwood executables are located in this repo in the "target/release" folder
- documentation about dogwood is located in this repo in the dogwood-docs