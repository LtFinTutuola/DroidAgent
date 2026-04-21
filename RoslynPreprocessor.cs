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
                string commandLine = Console.ReadLine();
                if (commandLine == null) break;

                var codeBuffer = new System.Text.StringBuilder();
                string line;
                while ((line = Console.ReadLine()) != null)
                {
                    if (line == Sentinel) break;
                    codeBuffer.AppendLine(line);
                }

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

                    if (commandLine.StartsWith("CLEAN"))
                    {
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
                    else if (commandLine.StartsWith("EXTRACT|"))
                    {
                        var lineParts = commandLine.Substring(8).Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries);
                        var targetLines = lineParts.Select(int.Parse).ToList();
                        var sourceText = tree.GetText();
                        var semanticNodes = new HashSet<SyntaxNode>();

                        foreach (var lineNum in targetLines)
                        {
                            // 1-based to 0-based
                            if (lineNum <= 0 || lineNum > sourceText.Lines.Count) continue;
                            var lineSpan = sourceText.Lines[lineNum - 1].Span;
                            var node = root.FindNode(lineSpan);

                            while (node != null && 
                                   !(node is MethodDeclarationSyntax) && 
                                   !(node is PropertyDeclarationSyntax) && 
                                   !(node is ConstructorDeclarationSyntax) && 
                                   !(node is ClassDeclarationSyntax))
                            {
                                node = node.Parent;
                            }
                            
                            if (node != null)
                            {
                                semanticNodes.Add(node);
                            }
                        }

                        var finalNodes = new HashSet<SyntaxNode>();
                        foreach (var n in semanticNodes)
                        {
                            if (n is ClassDeclarationSyntax classNode)
                            {
                                foreach (var member in classNode.Members)
                                {
                                    if (member is MethodDeclarationSyntax || 
                                        member is PropertyDeclarationSyntax || 
                                        member is ConstructorDeclarationSyntax)
                                    {
                                        finalNodes.Add(member);
                                    }
                                }
                            }
                            else
                            {
                                finalNodes.Add(n);
                            }
                        }

                        var chunks = new System.Collections.Generic.List<object>();
                        foreach (var n in finalNodes.OrderBy(x => x.SpanStart))
                        {
                            // Extract comments
                            var extractedComments = n.DescendantTrivia(descendIntoTrivia: true)
                                .Concat(n.GetLeadingTrivia())
                                .Concat(n.GetTrailingTrivia())
                                .Where(t => t.IsKind(SyntaxKind.SingleLineCommentTrivia) || 
                                            t.IsKind(SyntaxKind.MultiLineCommentTrivia) || 
                                            t.IsKind(SyntaxKind.SingleLineDocumentationCommentTrivia) ||
                                            t.IsKind(SyntaxKind.MultiLineDocumentationCommentTrivia) ||
                                            t.IsKind(SyntaxKind.DocumentationCommentExteriorTrivia))
                                .Select(t => t.ToString().Trim())
                                .Where(s => !string.IsNullOrWhiteSpace(s))
                                .Distinct()
                                .ToList();

                            var cleanNodeCode = n.WithoutTrivia().NormalizeWhitespace().ToFullString();

                            chunks.Add(new {
                                code = cleanNodeCode,
                                comments = extractedComments
                            });
                        }

                        Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(chunks));
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error processing [{commandLine}]: {ex.Message}");
                    Console.Error.WriteLine($"Error processing [{commandLine}]: {ex.Message}");
                }
                finally
                {
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
