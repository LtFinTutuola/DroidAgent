using System;
using System.IO;
using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace RoslynPreprocessor
{
    class Program
    {
        static void Main(string[] args)
        {
            var parseOptions = new CSharpParseOptions(
                preprocessorSymbols: new[] { "ANDROID", "MonoDroid" },
                languageVersion: LanguageVersion.Latest,
                documentationMode: DocumentationMode.Parse
            );
            var rewriter = new CleanRewriter();

            Console.InputEncoding = System.Text.Encoding.UTF8;
            Console.OutputEncoding = System.Text.Encoding.UTF8;

            // Sentinel used to mark end of input/output blocks
            const string Sentinel = "===END_OF_CODE===";

            // Notify Python that we are ready
            Console.WriteLine("READY");

            while (true)
            {
                string line;
                var codeBuffer = new System.Text.StringBuilder();
                
                // Read until sentinel or stream end
                while ((line = Console.ReadLine()) != null)
                {
                    if (line == Sentinel) break;
                    codeBuffer.AppendLine(line);
                }

                if (line == null && codeBuffer.Length == 0) break; // Finished

                string code = codeBuffer.ToString();
                if (string.IsNullOrWhiteSpace(code)) 
                {
                    Console.WriteLine(Sentinel);
                    continue;
                }

                try
                {
                    var tree = CSharpSyntaxTree.ParseText(code, parseOptions);
                    var root = tree.GetRoot();
                    
                    // 1. Remove all preprocessor directives and disabled code
                    var triviaToRemove = root.DescendantTrivia().Where(t => 
                        t.IsKind(SyntaxKind.DisabledTextTrivia) ||
                        t.IsKind(SyntaxKind.IfDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.ElifDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.ElseDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.EndIfDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.RegionDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.EndRegionDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.DefineDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.UndefDirectiveTrivia)
                    );
                    
                    root = root.ReplaceTrivia(triviaToRemove, (o, r) => default(SyntaxTrivia));
                    
                    // 2. Perform semantic-like pruning with the Rewriter
                    root = rewriter.Visit(root);
                    
                    // 3. Normalize whitespace to fix formatting after deletions
                    var finalCode = root.NormalizeWhitespace().ToFullString();
                    Console.WriteLine(finalCode);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error processing code: {ex.Message}");
                    Console.Error.WriteLine($"Error processing code: {ex.Message}");
                }
                finally
                {
                    // Always send sentinel back so Python knows we are done with this file
                    Console.WriteLine(Sentinel);
                }
            }
        }
    }
    
    class CleanRewriter : CSharpSyntaxRewriter
    {
        private bool IsDevExpress(string text)
        {
            return text != null && text.Contains("DevExpress");
        }

        public override SyntaxNode VisitUsingDirective(UsingDirectiveSyntax node)
        {
            if (node == null) return null;
            var name = node.Name.ToString();
            if (IsDevExpress(name)) return null;
            return base.VisitUsingDirective(node);
        }

        public override SyntaxNode VisitFieldDeclaration(FieldDeclarationSyntax node)
        {
            if (node == null) return null;
            if (node.Declaration != null && node.Declaration.Type != null && IsDevExpress(node.Declaration.Type.ToString())) return null;
            return base.VisitFieldDeclaration(node);
        }

        public override SyntaxNode VisitPropertyDeclaration(PropertyDeclarationSyntax node)
        {
            if (node == null) return null;
            if (node.Type != null && IsDevExpress(node.Type.ToString())) return null;
            return base.VisitPropertyDeclaration(node);
        }

        public override SyntaxNode VisitMethodDeclaration(MethodDeclarationSyntax node)
        {
            if (node == null) return null;
            // Prune methods that return DevExpress types or have DevExpress parameters
            if (node.ReturnType != null && IsDevExpress(node.ReturnType.ToString())) return null;
            if (node.ParameterList != null && node.ParameterList.Parameters.Any(p => p.Type != null && IsDevExpress(p.Type.ToString()))) return null;
            
            return base.VisitMethodDeclaration(node);
        }

        public override SyntaxNode VisitObjectCreationExpression(ObjectCreationExpressionSyntax node)
        {
            if (node == null) return null;
            if (node.Type != null && IsDevExpress(node.Type.ToString())) return null;
            return base.VisitObjectCreationExpression(node);
        }

        public override SyntaxNode VisitLocalDeclarationStatement(LocalDeclarationStatementSyntax node)
        {
            if (node == null) return null;
            if (node.Declaration != null && node.Declaration.Type != null && IsDevExpress(node.Declaration.Type.ToString())) return null;
            return base.VisitLocalDeclarationStatement(node);
        }
        
        public override SyntaxNode VisitExpressionStatement(ExpressionStatementSyntax node)
        {
            if (node == null) return null;
            if (node.Expression != null && IsDevExpress(node.Expression.ToString())) return null;
            return base.VisitExpressionStatement(node);
        }
    }
}
