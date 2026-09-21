# Specify the source files for the "src" module
set(SRC_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/main.c
    ${CMAKE_CURRENT_SOURCE_DIR}/../LLMForwardCommon/src/llm_common.c
)

# Make sources and include paths visible to the main CMakeLists.txt
set(SOURCES ${SRC_SOURCES} PARENT_SCOPE)
set(INCLUDE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/include PARENT_SCOPE)
