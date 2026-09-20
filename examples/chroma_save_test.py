# 功能说明：将PDF文件进行向量计算并持久化存储到向量数据库（chroma）
# 注意：这是教程原型脚本，不属于生产链路；所需依赖为 openai / chromadb / pdfminer.six
#      运行：python -m examples.chroma_save_test

# 相关依赖库
# pip install openai chromadb pdfminer.six


# 引入相关库
import os
import logging
from openai import OpenAI
import chromadb
import uuid
import numpy as np
from examples import pdf_split_zh as pdfSplitTest_Ch
from examples import pdf_split_en as pdfSplitTest_En


# 设置日志模版
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# 模型设置相关  根据自己的实际情况进行调整
API_TYPE = os.getenv("EMB_API_TYPE", "oneapi")  # openai:调用gpt模型；oneapi:调用oneapi方案支持的模型(这里调用通义千问)

# openai / oneapi 相关配置：一律从环境变量读取，禁止把密钥写进代码
# 需要在 .env 或系统环境变量中提供：
#   OPENAI_API_BASE / OPENAI_EMBEDDING_API_KEY / OPENAI_EMBEDDING_MODEL
#   ONEAPI_API_BASE / ONEAPI_EMBEDDING_API_KEY / ONEAPI_EMBEDDING_MODEL
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "")
OPENAI_EMBEDDING_API_KEY = os.getenv("OPENAI_EMBEDDING_API_KEY", "")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
ONEAPI_API_BASE = os.getenv("ONEAPI_API_BASE", "")
ONEAPI_EMBEDDING_API_KEY = os.getenv("ONEAPI_EMBEDDING_API_KEY", "")
ONEAPI_EMBEDDING_MODEL = os.getenv("ONEAPI_EMBEDDING_MODEL", "text-embedding-v1")

# 设置测试文本类型
TEXT_LANGUAGE = os.getenv("TEXT_LANGUAGE", "Chinese")  #Chinese 或 English
# TEXT_LANGUAGE = 'English'  #Chinese 或 English
# 测试的pdf文件路径（请自行放入 examples/input/ 或改成实际路径）
INPUT_PDF = os.getenv("INPUT_PDF", "examples/input/健康档案.pdf")
# INPUT_PDF = "examples/input/llama2.pdf"
# 指定文件中待处理的页码，全部页码则填None
PAGE_NUMBERS=None
# PAGE_NUMBERS=[2, 3]

# 指定向量数据库chromaDB的存储位置和集合 根据自己的实际情况进行调整
CHROMADB_DIRECTORY = os.getenv("CHROMADB_DIRECTORY", "chromaDB")  # chromaDB向量数据库的持久化路径
CHROMADB_COLLECTION_NAME = os.getenv("CHROMADB_COLLECTION_NAME", "demo001")  # 待查询的chromaDB向量数据库的集合名称


# get_embeddings方法计算向量
def get_embeddings(texts):
    global API_TYPE, ONEAPI_API_BASE, ONEAPI_EMBEDDING_API_KEY, ONEAPI_EMBEDDING_MODEL, OPENAI_API_BASE, OPENAI_EMBEDDING_API_KEY, ONEAPI_EMBEDDING_MODEL
    if API_TYPE == 'oneapi':
        try:
            # 初始化非OpenAI的Embedding模型,这里使用的是oneapi方案
            client = OpenAI(
                base_url=ONEAPI_API_BASE,
                api_key=ONEAPI_EMBEDDING_API_KEY
            )
            data = client.embeddings.create(input=texts,model=ONEAPI_EMBEDDING_MODEL).data
            return [x.embedding for x in data]
        except Exception as e:
            logger.info(f"生成向量时出错: {e}")
            return []

    elif API_TYPE == 'openai':
        try:
            # 初始化OpenAI的Embedding模型
            client = OpenAI(
                base_url=OPENAI_API_BASE,
                api_key=OPENAI_EMBEDDING_API_KEY
            )
            data = client.embeddings.create(input=texts,model=OPENAI_EMBEDDING_MODEL).data
            return [x.embedding for x in data]
        except Exception as e:
            logger.info(f"生成向量时出错: {e}")
            return []


# 对文本按批次进行向量计算
def generate_vectors(data, max_batch_size=25):
    results = []
    for i in range(0, len(data), max_batch_size):
        batch = data[i:i + max_batch_size]
        # 调用向量生成get_embeddings方法  根据调用的API不同进行选择
        response = get_embeddings(batch)
        results.extend(response)
    return results


# 封装向量数据库chromadb类，提供两种方法
class MyVectorDBConnector:
    def __init__(self, collection_name, embedding_fn):
        # 申明使用全局变量
        global CHROMADB_DIRECTORY
        # 实例化一个chromadb对象
        # 设置一个文件夹进行向量数据库的持久化存储  路径为当前文件夹下chromaDB文件夹
        chroma_client = chromadb.PersistentClient(path=CHROMADB_DIRECTORY)
        # 创建一个collection数据集合
        # get_or_create_collection()获取一个现有的向量集合，如果该集合不存在，则创建一个新的集合
        self.collection = chroma_client.get_or_create_collection(
            name=collection_name)
        # embedding处理函数
        self.embedding_fn = embedding_fn

    # 添加文档到集合
    # 文档通常包括文本数据和其对应的向量表示，这些向量可以用于后续的搜索和相似度计算
    def add_documents(self, documents):
        self.collection.add(
            embeddings=self.embedding_fn(documents),  # 调用函数计算出文档中文本数据对应的向量
            documents=documents,  # 文档的文本数据
            ids=[str(uuid.uuid4()) for i in range(len(documents))]  # 文档的唯一标识符 自动生成uuid,128位  
        )
        
    # 检索向量数据库，返回包含查询结果的对象或列表，这些结果包括最相似的向量及其相关信息
    # query：查询文本
    # top_n：返回与查询向量最相似的前 n 个向量
    def search(self, query, top_n):
        try:
            results = self.collection.query(
                # 计算查询文本的向量，然后将查询文本生成的向量在向量数据库中进行相似度检索
                query_embeddings=self.embedding_fn([query]),
                n_results=top_n
            )
            return results
        except Exception as e:
            logger.info(f"检索向量数据库时出错: {e}")
            return []


# 封装文本预处理及灌库方法  提供外部调用
def vectorStoreSave():
    global TEXT_LANGUAGE, CHROMADB_COLLECTION_NAME, INPUT_PDF, PAGE_NUMBERS
    # 测试中文文本
    if TEXT_LANGUAGE == 'Chinese':
        # 1、获取处理后的文本数据
        # 演示测试对指定的全部页进行处理，其返回值为划分为段落的文本列表
        paragraphs = pdfSplitTest_Ch.getParagraphs(
            filename=INPUT_PDF,
            page_numbers=PAGE_NUMBERS,
            min_line_length=1
        )
        # 2、将文本片段灌入向量数据库
        # 实例化一个向量数据库对象
        # 其中，传参collection_name为集合名称, embedding_fn为向量处理函数
        vector_db = MyVectorDBConnector(CHROMADB_COLLECTION_NAME, generate_vectors)
        # 向向量数据库中添加文档（文本数据、文本数据对应的向量数据）
        vector_db.add_documents(paragraphs)
        # 3、封装检索接口进行检索测试
        user_query = "张三九的基本信息是什么"
        # 将检索出的5个近似的结果
        search_results = vector_db.search(user_query, 5)
        logger.info(f"检索向量数据库的结果: {search_results}")


    # 测试英文文本
    elif TEXT_LANGUAGE == 'English':
        # 1、获取处理后的文本数据
        # 演示测试对指定的全部页进行处理，其返回值为划分为段落的文本列表
        paragraphs = pdfSplitTest_En.getParagraphs(
            filename=INPUT_PDF,
            page_numbers=PAGE_NUMBERS,
            min_line_length=1
        )
        # 2、将文本片段灌入向量数据库
        # 实例化一个向量数据库对象
        # 其中，传参collection_name为集合名称, embedding_fn为向量处理函数
        vector_db = MyVectorDBConnector(CHROMADB_COLLECTION_NAME, generate_vectors)
        # 向向量数据库中添加文档（文本数据、文本数据对应的向量数据）
        vector_db.add_documents(paragraphs)
        # 3、封装检索接口进行检索测试
        user_query = "llama2安全性如何"
        # 将检索出的5个近似的结果
        search_results = vector_db.search(user_query, 5)
        logger.info(f"检索向量数据库的结果: {search_results}")


if __name__ == "__main__":
    # 测试文本预处理及灌库
    vectorStoreSave()

