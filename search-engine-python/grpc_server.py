import grpc
from concurrent import futures
import time

# Auto-generated gRPC code from protobuf
import search_pb2
import search_pb2_grpc

from search_engine import HybridSearchEngine

class SearchServiceServicer(search_pb2_grpc.SearchServiceServicer):
    def __init__(self):
        # Initialize search engine upon server start
        print("Starting gRPC server... Initializing Search Engine...")
        self.engine = HybridSearchEngine(
            bm25_title_path="bm25_title.pkl",
            bm25_abstract_path="bm25_abstract.pkl",
            faiss_path="faiss.index",
            docs_path="docs.pkl"
        )
        print("Search Engine Initialized.")

    def Search(self, request, context):
        query = request.query

        response = search_pb2.SearchResponse()

        try:
            page = getattr(request, 'page', 1) or 1
            print(f"Received query: '{query}', page: {page}")

            results = self.engine.search(query, page=page)

            for doc in results:
                paper = response.papers.add()
                paper.id = str(doc.get("id", ""))
                paper.title = str(doc.get("title", ""))
                paper.abstract = str(doc.get("abstract", ""))

                authors = doc.get("authors", [])
                if isinstance(authors, str):
                    authors = [authors]

                paper.authors.extend([str(a) for a in authors])

                paper.pdf_url = str(doc.get("pdf_url", ""))

        except Exception as e:
            print(f"Error executing search: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))

        return response

    def Similar(self, request, context):
        paper_id = request.paper_id
        top_k = request.top_k or 10
        print(f"Received similar request for paper: '{paper_id}', top_k: {top_k}")
        
        response = search_pb2.SearchResponse()
        
        try:
            results = self.engine.similar(paper_id, top_k)
            
            for doc in results:
                paper = response.papers.add()
                paper.id = str(doc.get("id", ""))
                paper.title = str(doc.get("title", ""))
                paper.abstract = str(doc.get("abstract", ""))
                
                authors = doc.get("authors", [])
                if isinstance(authors, str):
                    authors = [authors]
                    
                paper.authors.extend([str(a) for a in authors])
                    
                paper.pdf_url = str(doc.get("pdf_url", ""))
                
        except Exception as e:
            print(f"Error executing similar search: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            
        return response

    def Autocomplete(self, request, context):
        prefix = request.prefix
        
        response = search_pb2.AutocompleteResponse()
        
        try:
            suggestions = self.engine.autocomplete(prefix)
            response.suggestions.extend(suggestions)
        except Exception as e:
            print(f"Error executing autocomplete: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            
        return response

def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    search_pb2_grpc.add_SearchServiceServicer_to_server(SearchServiceServicer(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    print("gRPC Server started on port 50051")
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        server.stop(0)

if __name__ == '__main__':
    serve()
